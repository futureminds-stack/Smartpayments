"""
Wallet / transfer system, backed by MongoDB Atlas (free-tier NoSQL).

Wallet balances and transaction history are intentionally kept out of the
main Postgres (Neon) database and stored in MongoDB instead - it's simple
key/value-ish data with no need for relational joins, and it lets the two
concerns (accounts vs. money movement) scale/reset independently.

Setup (free tier):
  1. Create a free cluster at https://www.mongodb.com/cloud/atlas/register
  2. Database Access -> add a user/password.
  3. Network Access -> allow access from anywhere (0.0.0.0/0) so Render can
     reach it (or add Render's static outbound IPs if you're on a paid plan).
  4. Copy the connection string ("mongodb+srv://...") and set it as the
     MONGODB_URI environment variable in Render. Include a database name in
     the URI path, e.g. ".../referral_platform?retryWrites=true&w=majority".

Every wallet is keyed by the user's Referral ID (users.public_user_id in
Postgres), so no foreign key / cross-database join is ever required - the
two databases stay fully decoupled.
"""

import os
import time
import uuid
import hashlib
import logging

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

MONGODB_URI = os.environ.get("MONGODB_URI")
INITIAL_BALANCE = 100.0


def wallet_address(referral_id):
    """Deterministic, crypto-exchange-style display address derived from
    the account's Referral ID (e.g. '0x9F3A1C...'). Purely cosmetic - the
    Referral ID itself is still what's actually stored and used for
    transfers under the hood, this just formats it the way people
    recognize from Binance/MetaMask/etc. Same input always produces the
    same output, so it never needs to be stored anywhere."""
    digest = hashlib.sha256(f"referralpro-wallet-{referral_id}".encode()).hexdigest()
    return "0x" + digest[:40].upper()

_client = None
_db = None


class WalletError(Exception):
    """Raised for expected, user-facing wallet problems (bad amount,
    insufficient balance, unknown recipient, etc). Callers should catch
    this separately from unexpected/infra errors."""
    pass


def _get_db():
    global _client, _db
    if _db is not None:
        return _db
    if not MONGODB_URI:
        raise RuntimeError(
            "MONGODB_URI environment variable is not set - wallet features "
            "are disabled. Add a free MongoDB Atlas connection string as "
            "MONGODB_URI in Render's environment variables."
        )
    _client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    _db = _client.get_default_database()
    _db.wallets.create_index("referral_id", unique=True)
    _db.transactions.create_index("created_at")
    _db.transactions.create_index("from_id")
    _db.transactions.create_index("to_id")
    return _db


def wallet_enabled():
    return bool(MONGODB_URI)


def init_wallet(referral_id, full_name=None):
    """Create a wallet with the starting balance if one doesn't exist yet.
    Safe to call repeatedly - existing wallets are left untouched."""
    db = _get_db()
    db.wallets.update_one(
        {"referral_id": referral_id},
        {
            "$setOnInsert": {
                "referral_id": referral_id,
                "full_name": full_name,
                "balance": INITIAL_BALANCE,
                "created_at": time.time(),
            }
        },
        upsert=True,
    )
    return get_wallet(referral_id)


def get_wallet(referral_id):
    db = _get_db()
    return db.wallets.find_one({"referral_id": referral_id}, {"_id": 0})


def get_transactions(referral_id, limit=20):
    db = _get_db()
    cursor = (
        db.transactions.find(
            {"$or": [{"from_id": referral_id}, {"to_id": referral_id}]},
            {"_id": 0},
        )
        .sort("created_at", -1)
        .limit(limit)
    )
    return list(cursor)


def get_all_transactions(limit=100):
    """Every wallet transfer platform-wide, most recent first — for the
    admin panel's payment history view."""
    db = _get_db()
    cursor = db.transactions.find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
    return list(cursor)


def credit_wallet(referral_id, amount, reason=None, full_name=None):
    """Add money to a wallet directly (no sender) — used for rewards like
    the referral bonus. Creates the wallet with the starting balance first
    if it doesn't exist yet, then adds `amount` on top."""
    if amount <= 0:
        raise WalletError("Amount must be greater than zero.")
    db = _get_db()
    init_wallet(referral_id, full_name=full_name)
    db.wallets.update_one({"referral_id": referral_id}, {"$inc": {"balance": amount}})
    db.transactions.insert_one(
        {
            "txn_id": uuid.uuid4().hex[:12].upper(),
            "from_id": "SYSTEM",
            "from_name": reason or "Reward",
            "to_id": referral_id,
            "to_name": full_name,
            "amount": amount,
            "created_at": time.time(),
        }
    )
    return get_wallet(referral_id)


def transfer(from_id, to_id, amount, from_name=None, to_name=None):
    """Move `amount` from from_id's wallet to to_id's wallet atomically.
    Raises WalletError for any user-facing validation failure."""
    to_id = (to_id or "").strip()
    if not to_id:
        raise WalletError("Enter a recipient Referral ID.")
    if to_id == from_id:
        raise WalletError("You can't transfer to your own wallet.")

    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise WalletError("Enter a valid amount.")
    if amount <= 0:
        raise WalletError("Amount must be greater than zero.")

    db = _get_db()

    if not db.wallets.find_one({"referral_id": to_id}):
        raise WalletError("Recipient Referral ID not found.")

    # Atomic conditional decrement - only succeeds if the balance is
    # sufficient, so two simultaneous transfers can't overdraw the wallet.
    sender = db.wallets.find_one_and_update(
        {"referral_id": from_id, "balance": {"$gte": amount}},
        {"$inc": {"balance": -amount}},
        return_document=ReturnDocument.AFTER,
    )
    if not sender:
        raise WalletError("Insufficient balance.")

    db.wallets.update_one({"referral_id": to_id}, {"$inc": {"balance": amount}})

    db.transactions.insert_one(
        {
            "txn_id": uuid.uuid4().hex[:12].upper(),
            "from_id": from_id,
            "from_name": from_name,
            "to_id": to_id,
            "to_name": to_name,
            "amount": amount,
            "created_at": time.time(),
        }
    )
    return sender["balance"]
