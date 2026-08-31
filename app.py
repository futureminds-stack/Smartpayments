
import os
import secrets
import re
import logging
import traceback
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import wallet

# ─── Logging ───────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Configuration ─────────────────────────────────────────
app = Flask(__name__)

# Render (like Heroku/Railway) terminates HTTPS at its edge and forwards
# requests to the app over plain HTTP, adding X-Forwarded-Proto/-Host
# headers. Without ProxyFix, Flask doesn't know the original request was
# HTTPS, so url_for(..., _external=True) - which builds the Google OAuth
# redirect_uri - generates an http:// URL instead of https://. That
# mismatches whatever https:// URI is registered in Google Cloud Console
# and breaks the OAuth round trip. This trusts the single reverse proxy
# in front of the app (Render's) to report the real scheme/host.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
if not os.environ.get("SECRET_KEY"):
    logger.warning("SECRET_KEY not set in environment - using an insecure default. "
                    "Set a real SECRET_KEY in Render's environment variables; without "
                    "it, sessions (including the Google OAuth login flow) can break "
                    "across deploys/restarts.")
# Only require HTTPS-only cookies in production. If this stays hardcoded True,
# the session cookie is silently never set on plain HTTP (e.g. local dev),
# which makes login look like it "succeeds" but immediately bounces back to
# the login page. Set SESSION_COOKIE_SECURE=false in the environment for
# local/non-HTTPS testing.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 3600  # 1 hour

# ─── CSRF protection ────────────────────────────────────────
# Every state-changing form in this app (login, register, password reset,
# admin approve/reject actions) is a plain POST with a session cookie and
# no anti-CSRF token. That means a malicious page anywhere on the web could
# have silently submitted a hidden form to, say, /admin/approve-user/7 while
# an admin was logged in and just browsing - the browser attaches the
# session cookie automatically. Flask-WTF's CSRFProtect requires a signed,
# per-session token on every POST/PUT/PATCH/DELETE (Jinja's `csrf_token()`,
# added to every form in the templates), rejecting anything without one.
csrf = CSRFProtect(app)

# ─── Rate limiting ──────────────────────────────────────────
# With only a handful of accounts, a brute-force or credential-stuffing
# script could try thousands of passwords against /login (or hammer
# /register, /forgot-password) with no pushback. Flask-Limiter caps requests
# per IP; storage is in-memory, which is fine for a single small
# gunicorn deployment like this one (set LIMITER_STORAGE_URI to a Redis URL
# if you ever scale to multiple dynos/instances, so the counters are shared).
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri=os.environ.get("LIMITER_STORAGE_URI", "memory://"),
    default_limits=[],
)

# ─── Security headers on every response ────────────────────
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # Only advertise HSTS when we're actually enforcing HTTPS cookies -
    # sending it while testing over plain HTTP would lock browsers into
    # HTTPS-only for this host, breaking local/dev access.
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    return response

# ─── Signed, expiring tokens for password reset links ────────
# (Previously the token was just base64("<user_id>:<public_id>") with no
# signature or expiry - since public_user_id is shown to the user in their
# dashboard/referral link, anyone could forge a "valid" reset link for any
# user whose public ID they know. This uses the app's secret key to sign
# the token and enforces a 1-hour expiry.)
reset_serializer = URLSafeTimedSerializer(app.secret_key, salt="password-reset")
RESET_TOKEN_MAX_AGE = 3600  # 1 hour

# ─── Database Pool ─────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Pool sizing: this pool is created once PER GUNICORN WORKER PROCESS, so the
# real number of connections opened against the database is roughly
# (DB_POOL_MAXCONN * number_of_gunicorn_workers). Neon's free/dev tiers cap
# concurrent connections fairly low, so a maxconn of 20 per worker used to
# make it easy to blow past that limit (seen as intermittent
# "FATAL: too many connections" errors) once more than a couple of workers
# were running. Defaults are now conservative and overridable via env vars.
# If you're on Neon, prefer the pooled connection string (hostname contains
# "-pooler") for DATABASE_URL - it multiplexes many app connections over far
# fewer real Postgres connections and tolerates this kind of pool much better.
DB_POOL_MINCONN = int(os.environ.get("DB_POOL_MINCONN", "1"))
DB_POOL_MAXCONN = int(os.environ.get("DB_POOL_MAXCONN", "5"))

try:
    db_pool = ThreadedConnectionPool(
        minconn=DB_POOL_MINCONN,
        maxconn=DB_POOL_MAXCONN,
        dsn=DATABASE_URL,
        sslmode="require",
        connect_timeout=10,       # fail fast instead of hanging a worker if Neon is unreachable/cold-starting
        keepalives=1,             # detect half-dead TCP sockets (e.g. after Neon auto-suspends/idles out
        keepalives_idle=30,       # a connection) instead of handing out one that will error on first use
        keepalives_interval=10,
        keepalives_count=3,
    )
    logger.info(f"Database pool created successfully (min={DB_POOL_MINCONN}, max={DB_POOL_MAXCONN})")
except Exception as e:
    logger.error(f"Database pool creation failed: {e}")
    raise

def get_db():
    """Check out a connection from the pool, verifying it's actually alive first.

    Neon (and most managed/serverless Postgres) can silently close idle
    connections - e.g. when the underlying compute auto-suspends - without
    the pool knowing. Without this check, a stale connection gets handed to
    a route, and the first query in that request fails with something like
    "OperationalError: server closed the connection unexpectedly", turning
    into a confusing 500 that had nothing to do with the route's own logic.
    A cheap SELECT 1 here catches that and transparently swaps in a fresh
    connection instead.
    """
    last_err = None
    for _ in range(2):
        conn = db_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return conn
        except Exception as e:
            last_err = e
            logger.warning(f"Discarding a dead pooled DB connection: {e}")
            try:
                db_pool.putconn(conn, close=True)
            except Exception:
                pass
    # Both attempts failed - let the caller's own try/except handle it
    logger.error(f"get_db: unable to obtain a healthy connection: {last_err}")
    return db_pool.getconn()

def release_db(conn):
    db_pool.putconn(conn)

# ─── Google OAuth client (registered once, not per-request) ──
google_oauth_client = None
if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
    try:
        from authlib.integrations.flask_client import OAuth
        _oauth = OAuth(app)
        google_oauth_client = _oauth.register(
            name="google",
            client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
            client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        logger.info("Google OAuth client registered")
    except Exception as e:
        logger.error(f"Google OAuth registration failed: {e}")
        google_oauth_client = None
else:
    logger.info("Google OAuth not configured (GOOGLE_CLIENT_ID/SECRET not set)")

# ─── Email (SMTP) - used to deliver password reset links ──────
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USERNAME or "no-reply@example.com")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Referral Platform")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

EMAIL_CONFIGURED = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD)
if EMAIL_CONFIGURED:
    logger.info(f"Email sending configured via {SMTP_HOST}:{SMTP_PORT}")
else:
    logger.warning("SMTP not configured (SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD missing) - "
                    "password reset links will NOT be emailed, only shown to the admin as a fallback.")

def send_email(to_email, subject, html_body, text_body=None):
    """Send an email via SMTP. Returns True on success, False on failure (never raises)."""
    if not EMAIL_CONFIGURED:
        logger.warning(f"send_email skipped (SMTP not configured) - would have sent '{subject}' to {to_email}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
        msg["To"] = to_email

        if text_body:
            msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            if SMTP_USE_TLS:
                server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, [to_email], msg.as_string())

        logger.info(f"Email sent: '{subject}' -> {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False

# ─── Helpers ─────────────────────────────────────────────────
def generate_public_id():
    """Unique 16-digit numeric code, used as the account's Referral ID."""
    return "".join(secrets.choice("0123456789") for _ in range(16))

def validate_phone(phone):
    """Exactly 10 digits, like a standard Indian mobile number."""
    phone = (phone or "").strip()
    if not re.match(r"^\d{10}$", phone):
        return None, "Phone number must be exactly 10 digits."
    return phone, None

def validate_pincode(pincode):
    pincode = (pincode or "").strip()
    if pincode and not re.match(r"^\d{6}$", pincode):
        return None, "Pincode must be 6 digits."
    return pincode, None

def build_address_fields(form):
    """Read the structured address fields from a submitted form and
    return (parts_dict, formatted_string, errors)."""
    door_no = form.get("door_no", "").strip()
    mandal = form.get("mandal", "").strip()
    district = form.get("district", "").strip()
    state = form.get("state", "").strip()
    pincode, pin_err = validate_pincode(form.get("pincode", ""))

    errors = []
    if pin_err:
        errors.append(pin_err)
    for label, val, maxlen in (("Door number", door_no, 50), ("Mandal", mandal, 100),
                                ("District", district, 100), ("State", state, 100)):
        if len(val) > maxlen:
            errors.append(f"{label} must be {maxlen} characters or fewer.")

    parts = {"door_no": door_no, "mandal": mandal, "district": district,
              "state": state, "pincode": pincode or ""}
    formatted = ", ".join(p for p in [door_no, mandal, district, state, pincode] if p)
    return parts, formatted, errors

def get_level(ref_count):
    if ref_count >= 50: return "Legend"
    elif ref_count >= 25: return "Diamond"
    elif ref_count >= 10: return "Gold"
    elif ref_count >= 5: return "Silver"
    elif ref_count >= 1: return "Bronze"
    return "Starter"

def format_currency(amount):
    return f"\u20b9{amount:,.2f}"

PASSWORD_MIN_LENGTH = 8

def password_errors(password):
    """Replaces the old 'at least 6 characters' rule, which let through
    things like 'aaaaaa'. Still short and usable, but now requires a mix
    that resists common brute-force wordlists."""
    errs = []
    if len(password) < PASSWORD_MIN_LENGTH:
        errs.append(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if not re.search(r"[A-Za-z]", password):
        errs.append("Password must include at least one letter.")
    if not re.search(r"\d", password):
        errs.append("Password must include at least one number.")
    return errs

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first.", "warning")
            return redirect(url_for("login"))
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT is_admin FROM users WHERE id = %s", (session["user_id"],))
                user = cur.fetchone()
                if not user or not user.get("is_admin"):
                    flash("Admin access required.", "danger")
                    return redirect(url_for("dashboard"))
        except Exception as e:
            logger.error(f"Admin check error: {e}")
            flash("Error checking admin status.", "danger")
            return redirect(url_for("dashboard"))
        finally:
            release_db(conn)
        return f(*args, **kwargs)
    return decorated_function

# ─── Context Processor ──────────────────────────────────────
@app.context_processor
def inject_globals():
    return {"now": datetime.now(timezone.utc), "format_currency": format_currency}

# ─── Routes ──────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

# ═════════════════════════════════════════════════════════════
# GOOGLE OAUTH - Fixed CSRF handling
# ═════════════════════════════════════════════════════════════
@app.route("/google-login")
def google_login():
    """Initiate Google OAuth login."""
    if not google_oauth_client:
        flash("Google OAuth is not configured. Please use email/password login.", "warning")
        return redirect(url_for("login"))

    try:
        redirect_uri = url_for("google_callback", _external=True)
        return google_oauth_client.authorize_redirect(redirect_uri)
    except Exception as e:
        logger.error(f"Google login init error: {e}\n{traceback.format_exc()}")
        flash("Google login is temporarily unavailable. Please use email/password.", "warning")
        return redirect(url_for("login"))

@app.route("/google-callback")
def google_callback():
    """Handle Google OAuth callback."""
    if not google_oauth_client:
        flash("Google OAuth is not configured. Please use email/password login.", "warning")
        return redirect(url_for("login"))

    try:
        token = google_oauth_client.authorize_access_token()
        user_info = token.get("userinfo")
        if not user_info:
            flash("Google authentication failed.", "danger")
            return redirect(url_for("login"))

        email = user_info.get("email", "").lower()
        name = user_info.get("name", "")
        google_id = user_info.get("sub", "")

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, status, public_user_id FROM users WHERE google_id = %s OR email = %s",
                          (google_id, email))
                existing = cur.fetchone()

                if existing:
                    if existing["status"] == "pending":
                        session["waiting_user_id"] = existing["id"]
                        session["waiting_public_id"] = existing["public_user_id"]
                        flash("Your account is pending approval.", "warning")
                        return redirect(url_for("waiting_approval"))
                    if existing["status"] == "rejected":
                        flash("Your account has been rejected.", "danger")
                        return redirect(url_for("login"))

                    cur.execute("UPDATE users SET last_login = NOW() WHERE id = %s", (existing["id"],))
                    conn.commit()
                    session["user_id"] = existing["id"]
                    session["public_id"] = existing["public_user_id"]
                    cur.execute("SELECT full_name, username, is_admin, user_level FROM users WHERE id = %s",
                              (existing["id"],))
                    u = cur.fetchone()
                    session["username"] = u["username"]
                    session["full_name"] = u["full_name"]
                    session["is_admin"] = u.get("is_admin", False)
                    session["level"] = u["user_level"]
                    flash(f"Welcome back, {u['full_name']}!", "success")
                    if session["is_admin"]:
                        return redirect(url_for("admin_dashboard"))
                    return redirect(url_for("dashboard"))

                session["google_email"] = email
                session["google_name"] = name
                session["google_id"] = google_id
                ref_code = request.args.get("ref", "").strip().upper()
                return redirect(url_for("complete_google_profile", ref=ref_code))
        finally:
            release_db(conn)

    except Exception as e:
        logger.error(f"Google callback error: {e}\n{traceback.format_exc()}")
        flash("Google login failed. Please use email/password or try again.", "danger")
        return redirect(url_for("login"))

# ═════════════════════════════════════════════════════════════
# REGISTRATION
# ═════════════════════════════════════════════════════════════
@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone, phone_err = validate_phone(request.form.get("phone", ""))
        addr_parts, address, addr_errors = build_address_fields(request.form)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        ref_code = request.form.get("referral_id", "").strip().upper()
        external_wallet_id = request.form.get("external_wallet_id", "").strip()

        errors = []
        if not full_name or len(full_name) < 2:
            errors.append("Full name is required (min 2 chars).")
        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            errors.append("Valid email is required.")
        if phone_err:
            errors.append(phone_err)
        errors.extend(addr_errors)
        if len(external_wallet_id) > 128:
            errors.append("Crypto wallet ID must be 128 characters or fewer.")
        if not username or len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        errors.extend(password_errors(password))
        if password != confirm_password:
            errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("register.html", ref_code=ref_code)

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id FROM users WHERE email = %s OR username = %s", (email, username))
                if cur.fetchone():
                    flash("Email or username already exists.", "danger")
                    return render_template("register.html", ref_code=ref_code)

                if external_wallet_id:
                    cur.execute("SELECT id FROM users WHERE external_wallet_id = %s", (external_wallet_id,))
                    if cur.fetchone():
                        flash("That crypto wallet ID is already registered to another account.", "danger")
                        return render_template("register.html", ref_code=ref_code)

                referrer_id = None
                if ref_code:
                    cur.execute("SELECT public_user_id FROM users WHERE public_user_id = %s AND status = 'approved'", (ref_code,))
                    ref_user = cur.fetchone()
                    if ref_user:
                        referrer_id = ref_user["public_user_id"]
                    else:
                        flash("Invalid referral ID. Continuing without referrer.", "warning")

                public_id = generate_public_id()
                while True:
                    cur.execute("SELECT id FROM users WHERE public_user_id = %s", (public_id,))
                    if not cur.fetchone():
                        break
                    public_id = generate_public_id()

                password_hash = generate_password_hash(password)
                cur.execute("""
                    INSERT INTO users 
                    (public_user_id, full_name, email, phone, address, door_no, mandal, district, state, pincode,
                     external_wallet_id, username, password_hash, referral_id, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', NOW())
                    RETURNING id, public_user_id
                """, (public_id, full_name, email, phone, address,
                      addr_parts["door_no"], addr_parts["mandal"], addr_parts["district"],
                      addr_parts["state"], addr_parts["pincode"], external_wallet_id or None,
                      username, password_hash, referrer_id))
                new_user = cur.fetchone()

                if referrer_id:
                    cur.execute("""
                        INSERT INTO referrals (referrer_id, referred_id, status, reward_amount)
                        VALUES (%s, %s, 'pending', 100.00)
                    """, (referrer_id, public_id))

                conn.commit()
                session["waiting_user_id"] = new_user["id"]
                session["waiting_public_id"] = public_id

                flash("Registration successful! Pending admin approval.", "success")
                return redirect(url_for("waiting_approval"))
        except Exception as e:
            conn.rollback()
            logger.error(f"Registration error: {e}")
            flash("An error occurred. Please try again.", "danger")
            return render_template("register.html", ref_code=ref_code)
        finally:
            release_db(conn)

    ref_code = request.args.get("ref", "").strip().upper()
    return render_template("register.html", ref_code=ref_code)

@app.route("/waiting-approval")
def waiting_approval():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if "waiting_user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT public_user_id, full_name, email, status, created_at 
                FROM users WHERE id = %s
            """, (session["waiting_user_id"],))
            user = cur.fetchone()
            if not user:
                session.pop("waiting_user_id", None)
                session.pop("waiting_public_id", None)
                return redirect(url_for("login"))

            if user["status"] == "approved":
                session.pop("waiting_user_id", None)
                session.pop("waiting_public_id", None)
                flash("Your account has been approved! Please login.", "success")
                return redirect(url_for("login"))

            return render_template("waiting.html", user=user)
    except Exception as e:
        logger.error(f"Waiting page error: {e}")
        flash("Error loading page.", "danger")
        return redirect(url_for("login"))
    finally:
        release_db(conn)

@app.route("/complete-google-profile", methods=["GET", "POST"])
def complete_google_profile():
    if "google_email" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        phone, phone_err = validate_phone(request.form.get("phone", ""))
        addr_parts, address, addr_errors = build_address_fields(request.form)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        ref_code = request.form.get("referral_id", "").strip().upper()
        external_wallet_id = request.form.get("external_wallet_id", "").strip()

        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if phone_err:
            errors.append(phone_err)
        errors.extend(addr_errors)
        if len(external_wallet_id) > 128:
            errors.append("Crypto wallet ID must be 128 characters or fewer.")
        if not username or len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        errors.extend(password_errors(password))
        if password != confirm_password:
            errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("complete_google_profile.html",
                                   email=session.get("google_email"),
                                   name=session.get("google_name", ""),
                                   ref_code=ref_code)

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id FROM users WHERE email = %s OR username = %s",
                          (session["google_email"], username))
                if cur.fetchone():
                    flash("Email or username already exists.", "danger")
                    return render_template("complete_google_profile.html",
                                           email=session.get("google_email"),
                                           name=session.get("google_name", ""),
                                           ref_code=ref_code)

                if external_wallet_id:
                    cur.execute("SELECT id FROM users WHERE external_wallet_id = %s", (external_wallet_id,))
                    if cur.fetchone():
                        flash("That crypto wallet ID is already registered to another account.", "danger")
                        return render_template("complete_google_profile.html",
                                               email=session.get("google_email"),
                                               name=session.get("google_name", ""),
                                               ref_code=ref_code)

                referrer_id = None
                if ref_code:
                    cur.execute("SELECT public_user_id FROM users WHERE public_user_id = %s AND status = 'approved'", (ref_code,))
                    ref_user = cur.fetchone()
                    if ref_user:
                        referrer_id = ref_user["public_user_id"]

                public_id = generate_public_id()
                while True:
                    cur.execute("SELECT id FROM users WHERE public_user_id = %s", (public_id,))
                    if not cur.fetchone():
                        break
                    public_id = generate_public_id()

                password_hash = generate_password_hash(password)
                cur.execute("""
                    INSERT INTO users 
                    (public_user_id, full_name, email, phone, address, door_no, mandal, district, state, pincode,
                     external_wallet_id, username, password_hash, google_id, referral_id, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', NOW())
                    RETURNING id, public_user_id
                """, (public_id, full_name, session["google_email"], phone, address,
                      addr_parts["door_no"], addr_parts["mandal"], addr_parts["district"],
                      addr_parts["state"], addr_parts["pincode"], external_wallet_id or None,
                      username, password_hash, session.get("google_id"), referrer_id))
                new_user = cur.fetchone()

                if referrer_id:
                    cur.execute("""
                        INSERT INTO referrals (referrer_id, referred_id, status, reward_amount)
                        VALUES (%s, %s, 'pending', 100.00)
                    """, (referrer_id, public_id))

                conn.commit()
                session.pop("google_email", None)
                session.pop("google_name", None)
                session.pop("google_id", None)
                session["waiting_user_id"] = new_user["id"]
                session["waiting_public_id"] = public_id

                flash("Profile completed! Waiting for admin approval.", "success")
                return redirect(url_for("waiting_approval"))
        except Exception as e:
            conn.rollback()
            logger.error(f"Google profile error: {e}")
            flash("An error occurred.", "danger")
        finally:
            release_db(conn)

    ref_code = request.args.get("ref", "").strip().upper()
    return render_template("complete_google_profile.html",
                           email=session.get("google_email"),
                           name=session.get("google_name", ""),
                           ref_code=ref_code)

# ═════════════════════════════════════════════════════════════
# LOGIN / LOGOUT - With better error handling
# ═════════════════════════════════════════════════════════════
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("8 per minute")
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.", "danger")
            return render_template("login.html")

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Check if users table has the required columns
                cur.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name = 'users'
                """)
                columns = [row["column_name"] for row in cur.fetchall()]

                # Build query based on available columns
                select_cols = ["id", "public_user_id", "full_name", "username", "password_hash"]
                optional_cols = ["status", "is_admin", "user_level", "referral_count", "amount_earned"]
                for col in optional_cols:
                    if col in columns:
                        select_cols.append(col)

                query = f"SELECT {', '.join(select_cols)} FROM users WHERE username = %s OR email = %s"
                cur.execute(query, (username, username))
                user = cur.fetchone()

                if not user:
                    flash("Invalid username or password.", "danger")
                    return render_template("login.html")

                if not check_password_hash(user["password_hash"], password):
                    logger.warning(f"Failed login attempt for '{username}' from {get_remote_address()}")
                    flash("Invalid username or password.", "danger")
                    return render_template("login.html")

                # Handle status column
                status = user.get("status", "approved")
                if status == "pending":
                    session["waiting_user_id"] = user["id"]
                    session["waiting_public_id"] = user["public_user_id"]
                    flash("Your account is still pending approval.", "warning")
                    return redirect(url_for("waiting_approval"))

                if status == "rejected":
                    flash("Your account has been rejected. Contact support.", "danger")
                    return render_template("login.html")

                # Update last login
                if "last_login" in columns:
                    cur.execute("UPDATE users SET last_login = NOW() WHERE id = %s", (user["id"],))
                    conn.commit()

                session["user_id"] = user["id"]
                session["public_id"] = user["public_user_id"]
                session["username"] = user["username"]
                session["full_name"] = user.get("full_name", user["username"])
                session["is_admin"] = user.get("is_admin", False)
                session["level"] = user.get("user_level", "Starter")

                flash(f"Welcome back, {session['full_name']}!", "success")

                if session["is_admin"]:
                    return redirect(url_for("admin_dashboard"))
                return redirect(url_for("dashboard"))

        except Exception as e:
            logger.error(f"Login error: {e}")
            flash("An error occurred during login. Please try again.", "danger")
        finally:
            release_db(conn)

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

# ═════════════════════════════════════════════════════════════
# DASHBOARD
# ═════════════════════════════════════════════════════════════
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT u.*, 
                       r.public_user_id as referrer_public_id,
                       r.full_name as referrer_name
                FROM users u
                LEFT JOIN users r ON u.referral_id = r.public_user_id
                WHERE u.id = %s
            """, (session["user_id"],))
            user = cur.fetchone()

            if not user:
                session.clear()
                return redirect(url_for("login"))

            cur.execute("""
                SELECT full_name, public_user_id, status, created_at, approved_at
                FROM users
                WHERE referral_id = %s AND status = 'approved'
                ORDER BY approved_at DESC
                LIMIT 10
            """, (user["public_user_id"],))
            referrals = cur.fetchall()

            ref_link = request.host_url.rstrip("/") + url_for("register") + f"?ref={user['public_user_id']}"
            session["level"] = user.get("user_level", "Starter")

            try:
                my_wallet = wallet.get_wallet(user["public_user_id"]) or \
                            wallet.init_wallet(user["public_user_id"], user["full_name"])
                wallet_txns = wallet.get_transactions(user["public_user_id"])
            except Exception as e:
                logger.error(f"Wallet load error for {user['public_user_id']}: {e}")
                my_wallet = None
                wallet_txns = []

            return render_template("dashboard.html",
                                   user=user,
                                   referrals=referrals,
                                   ref_link=ref_link,
                                   wallet=my_wallet,
                                   wallet_txns=wallet_txns,
                                   wallet_id_display=wallet.wallet_address(user["public_user_id"]),
                                   wallet_enabled=wallet.wallet_enabled())
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        flash("Error loading dashboard.", "danger")
        return redirect(url_for("login"))
    finally:
        release_db(conn)

# ═════════════════════════════════════════════════════════════
# EDIT PROFILE (saves straight to the database on submit - no
# separate admin approval step; that only gates new registrations)
# ═════════════════════════════════════════════════════════════
@app.route("/profile/edit", methods=["GET", "POST"])
def edit_profile():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if request.method == "POST":
                full_name = request.form.get("full_name", "").strip()
                phone, phone_err = validate_phone(request.form.get("phone", ""))
                addr_parts, address, addr_errors = build_address_fields(request.form)
                external_wallet_id = request.form.get("external_wallet_id", "").strip()

                errors = []
                if not full_name or len(full_name) < 2:
                    errors.append("Full name is required (min 2 chars).")
                if phone_err:
                    errors.append(phone_err)
                errors.extend(addr_errors)
                if len(external_wallet_id) > 128:
                    errors.append("Crypto wallet ID must be 128 characters or fewer.")

                if errors:
                    for e in errors:
                        flash(e, "danger")
                    return redirect(url_for("edit_profile"))

                if external_wallet_id:
                    cur.execute("SELECT id FROM users WHERE external_wallet_id = %s AND id != %s",
                                (external_wallet_id, session["user_id"]))
                    if cur.fetchone():
                        flash("That crypto wallet ID is already registered to another account.", "danger")
                        return redirect(url_for("edit_profile"))

                cur.execute("""
                    UPDATE users
                    SET full_name = %s, phone = %s, address = %s,
                        door_no = %s, mandal = %s, district = %s, state = %s, pincode = %s,
                        external_wallet_id = %s
                    WHERE id = %s
                """, (full_name, phone, address,
                      addr_parts["door_no"], addr_parts["mandal"], addr_parts["district"],
                      addr_parts["state"], addr_parts["pincode"], external_wallet_id or None,
                      session["user_id"]))
                conn.commit()
                session["full_name"] = full_name
                flash("Profile updated and saved.", "success")
                return redirect(url_for("dashboard"))

            cur.execute("SELECT * FROM users WHERE id = %s", (session["user_id"],))
            user = cur.fetchone()
            if not user:
                session.clear()
                return redirect(url_for("login"))
            return render_template("edit_profile.html", user=user)
    except Exception as e:
        conn.rollback()
        logger.error(f"Edit profile error: {e}")
        flash("Could not update profile. Please try again.", "danger")
        return redirect(url_for("dashboard"))
    finally:
        release_db(conn)

# ═════════════════════════════════════════════════════════════
# WALLET (balances + transfers live in MongoDB - see wallet.py)
# ═════════════════════════════════════════════════════════════
@app.route("/wallet/transfer", methods=["POST"])
@limiter.limit("20 per hour")
def wallet_transfer():
    if "user_id" not in session:
        return redirect(url_for("login"))

    to_ref_id = request.form.get("to_referral_id", "").strip()
    amount = request.form.get("amount", "").strip()

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT public_user_id, full_name FROM users WHERE id = %s",
                        (session["user_id"],))
            me = cur.fetchone()
            if not me:
                session.clear()
                return redirect(url_for("login"))

            cur.execute("SELECT full_name FROM users WHERE public_user_id = %s", (to_ref_id,))
            recipient = cur.fetchone()

        new_balance = wallet.transfer(
            me["public_user_id"], to_ref_id, amount,
            from_name=me["full_name"],
            to_name=recipient["full_name"] if recipient else None,
        )
        flash(f"Sent {format_currency(float(amount))} to {to_ref_id}. "
              f"New balance: {format_currency(new_balance)}.", "success")
    except wallet.WalletError as e:
        flash(str(e), "warning")
    except Exception as e:
        logger.error(f"Wallet transfer error: {e}")
        flash("Wallet transfer failed. Please try again later.", "danger")
    finally:
        release_db(conn)

    return redirect(url_for("dashboard"))

# ═════════════════════════════════════════════════════════════
# FORGOT PASSWORD / RESET PASSWORD
# ═════════════════════════════════════════════════════════════
@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def forgot_password():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()

        if not identifier:
            flash("Please enter your username or email.", "danger")
            return render_template("forgot_password.html")

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, public_user_id, full_name, email, status
                    FROM users WHERE username = %s OR email = %s
                """, (identifier, identifier))
                user = cur.fetchone()

                if not user:
                    flash("If your account exists, a reset request has been sent to admin.", "info")
                    return render_template("forgot_password.html")

                if user.get("status", "approved") != "approved":
                    flash("Your account is not approved yet.", "warning")
                    return render_template("forgot_password.html")

                cur.execute("""
                    SELECT id FROM password_reset_requests 
                    WHERE user_id = %s AND status = 'pending'
                """, (user["id"],))
                if cur.fetchone():
                    flash("You already have a pending reset request.", "warning")
                    return render_template("forgot_password.html")

                cur.execute("""
                    INSERT INTO password_reset_requests (user_id, status, requested_at)
                    VALUES (%s, 'pending', NOW())
                """, (user["id"],))

                cur.execute("""
                    SELECT column_name FROM information_schema.columns WHERE table_name = 'users'
                """)
                users_columns = [c["column_name"] for c in cur.fetchall()]
                if "password_reset_status" in users_columns:
                    cur.execute("""
                        UPDATE users SET password_reset_status = 'requested' WHERE id = %s
                    """, (user["id"],))

                conn.commit()
                flash("Password reset request sent to admin for approval.", "success")
                return render_template("forgot_password.html", requested=True)

        except Exception as e:
            conn.rollback()
            logger.error(f"Forgot password error: {e}")
            flash("An error occurred.", "danger")
        finally:
            release_db(conn)

    return render_template("forgot_password.html")

@app.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def reset_password(token):
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                data = reset_serializer.loads(token, max_age=RESET_TOKEN_MAX_AGE)
                user_id = int(data["user_id"])
                public_id = data["public_id"]
            except SignatureExpired:
                flash("This reset link has expired. Please request a new one.", "danger")
                return redirect(url_for("login"))
            except (BadSignature, KeyError, ValueError, TypeError):
                flash("Invalid reset link.", "danger")
                return redirect(url_for("login"))

            cur.execute("""
                SELECT id, public_user_id, full_name, password_reset_status
                FROM users
                WHERE id = %s AND public_user_id = %s
            """, (user_id, public_id))
            user = cur.fetchone()

            if not user:
                flash("Invalid or expired reset link.", "danger")
                return redirect(url_for("login"))

            if user.get("password_reset_status") != 'approved':
                flash("This reset link is not approved or has expired.", "danger")
                return redirect(url_for("login"))

            if request.method == "POST":
                password = request.form.get("password", "")
                confirm = request.form.get("confirm_password", "")

                pw_errors = password_errors(password)
                if pw_errors:
                    for e in pw_errors:
                        flash(e, "danger")
                    return render_template("reset_password.html", token=token, user=user)
                if password != confirm:
                    flash("Passwords do not match.", "danger")
                    return render_template("reset_password.html", token=token, user=user)

                password_hash = generate_password_hash(password)
                cur.execute("""
                    UPDATE users 
                    SET password_hash = %s, password_reset_status = NULL, updated_at = NOW()
                    WHERE id = %s
                """, (password_hash, user["id"]))

                cur.execute("""
                    UPDATE password_reset_requests 
                    SET status = 'completed', resolved_at = NOW()
                    WHERE user_id = %s AND status = 'approved'
                """, (user["id"],))

                conn.commit()
                flash("Password reset successful! Please login.", "success")
                return redirect(url_for("login"))

            return render_template("reset_password.html", token=token, user=user)
    except Exception as e:
        logger.error(f"Reset password error: {e}")
        flash("Error processing reset.", "danger")
        return redirect(url_for("login"))
    finally:
        release_db(conn)

# ═════════════════════════════════════════════════════════════
# ADMIN DASHBOARD
# ═════════════════════════════════════════════════════════════
@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Check available columns
            cur.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'users'
            """)
            user_cols = [row["column_name"] for row in cur.fetchall()]

            # Build stats query
            has_status = "status" in user_cols
            has_amount = "amount_earned" in user_cols

            if has_status and has_amount:
                cur.execute("""
                    WITH stats AS (
                        SELECT 
                            COUNT(*) FILTER (WHERE status = 'pending') as pending_count,
                            COUNT(*) FILTER (WHERE status = 'approved') as approved_count,
                            COUNT(*) FILTER (WHERE status = 'rejected') as rejected_count,
                            COUNT(*) as total_count,
                            COUNT(*) FILTER (WHERE DATE(created_at) = CURRENT_DATE) as today_count,
                            COALESCE(SUM(amount_earned), 0) as total_earnings
                        FROM users
                    ),
                    reset_stats AS (
                        SELECT COUNT(*) as reset_count 
                        FROM password_reset_requests 
                        WHERE status = 'pending'
                    )
                    SELECT * FROM stats, reset_stats
                """)
            else:
                cur.execute("""
                    SELECT 
                        0 as pending_count,
                        COUNT(*) as approved_count,
                        0 as rejected_count,
                        COUNT(*) as total_count,
                        0 as today_count,
                        0 as total_earnings,
                        0 as reset_count
                    FROM users
                """)
            stats = cur.fetchone()

            if has_status:
                cur.execute("""
                    SELECT id, public_user_id, full_name, email, phone, address,
                           username, referral_id, created_at
                    FROM users WHERE status = 'pending'
                    ORDER BY created_at DESC
                """)
                pending_users = cur.fetchall()

                cur.execute("""
                    SELECT id, public_user_id, full_name, email, phone,
                           username, referral_count, amount_earned, user_level,
                           approved_at, created_at
                    FROM users WHERE status = 'approved'
                    ORDER BY referral_count DESC NULLS LAST, approved_at DESC NULLS LAST
                    LIMIT 20
                """)
                approved_users = cur.fetchall()
            else:
                pending_users = []
                cur.execute("""
                    SELECT id, public_user_id, full_name, email, phone,
                           username, referral_count, amount_earned, user_level,
                           approved_at, created_at
                    FROM users
                    ORDER BY referral_count DESC NULLS LAST, created_at DESC
                    LIMIT 20
                """)
                approved_users = cur.fetchall()

            cur.execute("""
                SELECT pr.id, pr.user_id, pr.status, pr.requested_at,
                       u.public_user_id, u.full_name, u.email, u.username
                FROM password_reset_requests pr
                JOIN users u ON pr.user_id = u.id
                WHERE pr.status = 'pending'
                ORDER BY pr.requested_at DESC
            """)
            reset_requests = cur.fetchall()

            # Resolved reset requests (approved/rejected), most recent first -
            # gives admins an audit trail of who reset whose password and when.
            cur.execute("""
                SELECT pr.id, pr.user_id, pr.status, pr.requested_at, pr.resolved_at,
                       u.public_user_id, u.full_name, u.email, u.username,
                       admin_u.full_name AS resolved_by_name
                FROM password_reset_requests pr
                JOIN users u ON pr.user_id = u.id
                LEFT JOIN users admin_u ON pr.resolved_by = admin_u.id
                WHERE pr.status IN ('approved', 'rejected')
                ORDER BY pr.resolved_at DESC NULLS LAST
                LIMIT 50
            """)
            reset_history = cur.fetchall()

            return render_template("admin.html",
                                   stats=stats,
                                   pending_users=pending_users,
                                   approved_users=approved_users,
                                   reset_requests=reset_requests,
                                   reset_history=reset_history,
                                   compose_email=session.pop("pending_compose_email", None))
    except Exception as e:
        logger.error(f"Admin dashboard error: {e}")
        flash("Error loading admin dashboard.", "danger")
        return redirect(url_for("dashboard"))
    finally:
        release_db(conn)

@app.route("/admin/approve-user/<int:user_id>", methods=["POST"])
@admin_required
def approve_user(user_id):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT public_user_id, referral_id, status FROM users WHERE id = %s", (user_id,))
            user = cur.fetchone()

            if not user:
                flash("User not found.", "danger")
                return redirect(url_for("admin_dashboard"))

            if user.get("status") != "pending":
                flash("User is not pending approval.", "warning")
                return redirect(url_for("admin_dashboard"))

            cur.execute("""
                UPDATE users 
                SET status = 'approved', approved_at = NOW(), updated_at = NOW()
                WHERE id = %s
                RETURNING public_user_id, referral_id, referral_count, amount_earned, user_level
            """, (user_id,))
            updated = cur.fetchone()

            if updated and updated.get("referral_id"):
                cur.execute("""
                    UPDATE users 
                    SET referral_count = referral_count + 1,
                        amount_earned = amount_earned + 100.00,
                        user_level = calculate_user_level(referral_count + 1)
                    WHERE public_user_id = %s
                """, (updated["referral_id"],))

                cur.execute("""
                    UPDATE referrals SET status = 'approved', approved_at = NOW()
                    WHERE referred_id = %s
                """, (updated["public_user_id"],))

            cur.execute("""
                INSERT INTO admin_logs (admin_id, action, target_user_id, details, created_at)
                VALUES (%s, 'approve_user', %s, %s, NOW())
            """, (session["user_id"], user_id, f"Approved user {updated['public_user_id']}"))

            conn.commit()
            flash(f"User {updated['public_user_id']} approved!", "success")

            if updated and updated.get("referral_id"):
                flash(f"Referral reward of {format_currency(100.00)} processed.", "info")

            # Give the newly-approved account its starting wallet balance
            # (wallet data lives in MongoDB, not Postgres - see wallet.py).
            try:
                wallet.init_wallet(updated["public_user_id"])
            except Exception as e:
                logger.error(f"Wallet init error for {updated['public_user_id']}: {e}")

            # Credit the referrer's actual wallet balance with the ₹100
            # bonus. The Postgres `amount_earned` update above is just a
            # display stat - this is what actually moves spendable money.
            if updated and updated.get("referral_id"):
                try:
                    wallet.credit_wallet(updated["referral_id"], 100.00,
                                          reason="Referral bonus")
                except Exception as e:
                    logger.error(f"Referral wallet credit error for {updated['referral_id']}: {e}")

    except Exception as e:
        conn.rollback()
        logger.error(f"Approve user error: {e}")
        flash("Error approving user.", "danger")
    finally:
        release_db(conn)

    return redirect(url_for("admin_dashboard"))

@app.route("/admin/reject-user/<int:user_id>", methods=["POST"])
@admin_required
def reject_user(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET status = 'rejected', updated_at = NOW() WHERE id = %s", (user_id,))
            cur.execute("""
                INSERT INTO admin_logs (admin_id, action, target_user_id, details, created_at)
                VALUES (%s, 'reject_user', %s, %s, NOW())
            """, (session["user_id"], user_id, f"Rejected user {user_id}"))
            conn.commit()
            flash("User rejected.", "info")
    except Exception as e:
        conn.rollback()
        logger.error(f"Reject user error: {e}")
        flash("Error rejecting user.", "danger")
    finally:
        release_db(conn)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/approve-reset/<int:request_id>", methods=["POST"])
@admin_required
def approve_reset(request_id):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT pr.id, pr.user_id, u.public_user_id, u.email, u.full_name
                FROM password_reset_requests pr
                JOIN users u ON pr.user_id = u.id
                WHERE pr.id = %s AND pr.status = 'pending'
            """, (request_id,))
            req = cur.fetchone()

            if not req:
                flash("Reset request not found.", "warning")
                return redirect(url_for("admin_dashboard"))

            cur.execute("""
                UPDATE password_reset_requests 
                SET status = 'approved', resolved_at = NOW(), resolved_by = %s
                WHERE id = %s
            """, (session["user_id"], request_id))

            cur.execute("""
                UPDATE users SET password_reset_status = 'approved' WHERE id = %s
            """, (req["user_id"],))

            token = reset_serializer.dumps({"user_id": req["user_id"], "public_id": req["public_user_id"]})
            reset_link = request.host_url.rstrip("/") + url_for("reset_password", token=token)

            cur.execute("""
                INSERT INTO admin_logs (admin_id, action, target_user_id, details, created_at)
                VALUES (%s, 'approve_reset', %s, %s, NOW())
            """, (session["user_id"], req["user_id"], f"Approved reset for {req['public_user_id']}"))

            conn.commit()

            # ── Compose, don't send ──────────────────────────────────────
            # This used to email the reset link straight to the user via
            # SMTP. That meant the admin never saw or reviewed what was
            # about to be sent, and it silently depended on SMTP_* being
            # configured correctly. Instead, we now prefill the recipient,
            # subject, and body and hand it back to the admin - they review
            # it in the compose modal on the admin dashboard and actually
            # hit "send" themselves (via their own email app, or by copying
            # the text into whatever channel they prefer). Nothing is sent
            # by the server in this flow.
            subject = "Your Password Reset Link"
            text_body = (
                f"Hi {req['full_name']},\n\n"
                f"Your password reset request has been approved. Use this link to set a new password "
                f"(expires in 1 hour, single use):\n{reset_link}\n\n"
                f"If you didn't request this, you can ignore this message."
            )
            session["pending_compose_email"] = {
                "to": req["email"],
                "subject": subject,
                "body": text_body,
                "recipient_name": req["full_name"],
                "reset_link": reset_link,
            }
            flash(f"Reset approved for {req['full_name']}. Review the email below and send it.", "success")

    except Exception as e:
        conn.rollback()
        logger.error(f"Approve reset error: {e}")
        flash("Error approving reset.", "danger")
    finally:
        release_db(conn)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/reject-reset/<int:request_id>", methods=["POST"])
@admin_required
def reject_reset(request_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE password_reset_requests 
                SET status = 'rejected', resolved_at = NOW(), resolved_by = %s
                WHERE id = %s
            """, (session["user_id"], request_id))
            conn.commit()
            flash("Password reset request rejected.", "info")
    except Exception as e:
        conn.rollback()
        logger.error(f"Reject reset error: {e}")
        flash("Error rejecting reset.", "danger")
    finally:
        release_db(conn)
    return redirect(url_for("admin_dashboard"))

# ═════════════════════════════════════════════════════════════
# REFERRAL NETWORK (hexagonal, live, interactive)
# ═════════════════════════════════════════════════════════════
def _node(row, has_children=False):
    """Serialize a users row into the compact shape the network UI expects."""
    return {
        "id": row["public_user_id"],
        "name": row["full_name"],
        "status": row.get("status") or "approved",
        "level": row.get("user_level") or "Starter",
        "referrals": row.get("referral_count") or 0,
        "earned": float(row.get("amount_earned") or 0),
        "joined": row["created_at"].strftime("%d %b %Y") if row.get("created_at") else None,
        "hasChildren": bool(has_children),
    }

def _children_map(cur, public_ids):
    """One query to fetch the direct referrals of every id in public_ids,
    grouped by referrer. Avoids N+1 queries when rendering several hexes
    (each with their own satellites) at once."""
    if not public_ids:
        return {}
    cur.execute("""
        SELECT id, public_user_id, full_name, status, user_level, referral_count,
               amount_earned, created_at, referral_id,
               EXISTS(SELECT 1 FROM users c WHERE c.referral_id = users.public_user_id) AS has_children
        FROM users
        WHERE referral_id = ANY(%s) AND is_admin = FALSE
        ORDER BY created_at ASC
    """, (public_ids,))
    grouped = {pid: [] for pid in public_ids}
    for row in cur.fetchall():
        grouped[row["referral_id"]].append(_node(row, row["has_children"]))
    return grouped

def _build_forest(cur, root_rows, max_depth=5):
    """Build the full nested n-ary tree (not just one ring of direct
    referrals) for a batch of root rows, going up to max_depth levels deep.
    Queries are batched per level across every tree at once (one query per
    depth, not one per node), so this stays cheap even with many trees."""
    forest = []
    id_to_treenode = {}
    for r in root_rows:
        tn = {"node": _node(r, r["has_children"]), "children": []}
        forest.append(tn)
        id_to_treenode[r["public_user_id"]] = tn

    current_ids = [r["public_user_id"] for r in root_rows]
    for _ in range(max_depth):
        if not current_ids:
            break
        kids_map = _children_map(cur, current_ids)
        next_ids = []
        for pid in current_ids:
            parent_tn = id_to_treenode[pid]
            for kid in kids_map.get(pid, []):
                child_tn = {"node": kid, "children": []}
                parent_tn["children"].append(child_tn)
                id_to_treenode[kid["id"]] = child_tn
                next_ids.append(kid["id"])
        current_ids = next_ids
    return forest

@app.route("/admin/wallets")
@admin_required
def admin_wallets():
    """Dedicated page listing every account's Wallet ID/address (copyable)
    plus every wallet-to-wallet transaction platform-wide."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT public_user_id, full_name, email, status, external_wallet_id
                FROM users
                WHERE status = 'approved'
                ORDER BY full_name ASC
            """)
            users = cur.fetchall()

        wallets_view = []
        for u in users:
            try:
                w = wallet.get_wallet(u["public_user_id"])
            except Exception as e:
                logger.error(f"admin_wallets: could not load wallet for {u['public_user_id']}: {e}")
                w = None
            wallets_view.append({
                "full_name": u["full_name"],
                "email": u["email"],
                "referral_id": u["public_user_id"],
                "wallet_address": wallet.wallet_address(u["public_user_id"]),
                "external_wallet_id": u["external_wallet_id"],
                "balance": w["balance"] if w else None,
            })

        try:
            payment_history = wallet.get_all_transactions(limit=200)
            for t in payment_history:
                ts = t.get("created_at")
                t["created_at_display"] = (
                    datetime.fromtimestamp(ts).strftime("%d %b %Y, %I:%M %p") if ts else "-"
                )
        except Exception as e:
            logger.error(f"admin_wallets: payment history load error: {e}")
            payment_history = []

        return render_template("admin_wallets.html",
                               wallets_view=wallets_view,
                               payment_history=payment_history,
                               wallet_enabled=wallet.wallet_enabled())
    except Exception as e:
        logger.error(f"Admin wallets page error: {e}")
        flash("Could not load wallets page.", "danger")
        return redirect(url_for("admin_dashboard"))
    finally:
        release_db(conn)

@app.route("/admin/network")
@admin_required
def admin_network():
    return render_template("network.html")

@app.route("/admin/api/network")
@admin_required
def admin_network_api():
    q = request.args.get("q", "").strip()
    center = request.args.get("center", "").strip().upper()
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if q:
                id_clause = "OR public_user_id ILIKE %s" if len(q) >= 4 else ""
                params = [f"%{q}%", f"%{q}%"] + ([f"%{q}%"] if len(q) >= 4 else [])
                cur.execute(f"""
                    SELECT public_user_id, full_name, status, user_level, referral_count,
                           amount_earned, created_at,
                           EXISTS(SELECT 1 FROM users c WHERE c.referral_id = users.public_user_id) AS has_children
                    FROM users
                    WHERE is_admin = FALSE AND (full_name ILIKE %s OR username ILIKE %s {id_clause})
                    ORDER BY full_name ASC LIMIT 15
                """, params)
                results = [_node(r, r["has_children"]) for r in cur.fetchall()]
                return jsonify({"mode": "search", "results": results})

            if center:
                cur.execute("""
                    SELECT id, public_user_id, full_name, status, user_level, referral_count,
                           amount_earned, created_at, referral_id,
                           EXISTS(SELECT 1 FROM users c WHERE c.referral_id = users.public_user_id) AS has_children
                    FROM users WHERE public_user_id = %s AND is_admin = FALSE
                """, (center,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "not_found"}), 404

                referrer = None
                if row["referral_id"]:
                    cur.execute("SELECT public_user_id, full_name FROM users WHERE public_user_id = %s",
                                (row["referral_id"],))
                    up = cur.fetchone()
                    if up:
                        referrer = {"id": up["public_user_id"], "name": up["full_name"]}

                satellites = _children_map(cur, [row["public_user_id"]]).get(row["public_user_id"], [])
                return jsonify({
                    "mode": "center",
                    "center": _node(row, row["has_children"]),
                    "referrer": referrer,
                    "satellites": satellites,
                })

            # Default: overview of every independent referral tree (root = no upline).
            # Roots with an actual downline become full n-ary tree diagrams;
            # roots with no referrer AND no referrals of their own are pulled
            # out into a separate "solo" list so the main canvas stays
            # readable instead of filling up with hundreds of single dots.
            cur.execute("""
                SELECT id, public_user_id, full_name, status, user_level, referral_count,
                       amount_earned, created_at,
                       EXISTS(SELECT 1 FROM users c WHERE c.referral_id = users.public_user_id) AS has_children
                FROM users
                WHERE referral_id IS NULL AND is_admin = FALSE
                ORDER BY referral_count DESC, created_at ASC
                LIMIT 200
            """)
            roots = cur.fetchall()
            tree_roots = [r for r in roots if r["has_children"]][:40]
            solo_roots = [r for r in roots if not r["has_children"]]

            forest = _build_forest(cur, tree_roots, max_depth=5)
            solo = [_node(r, False) for r in solo_roots]

            cur.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = FALSE")
            total_users = cur.fetchone()["c"]

            return jsonify({
                "mode": "overview",
                "trees": forest,
                "solo": solo,
                "totalRoots": len(roots),
                "totalUsers": total_users,
                "generatedAt": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        logger.error(f"Network API error: {e}")
        return jsonify({"error": "server_error"}), 500
    finally:
        release_db(conn)

@app.route("/api/my-network-levels")
def my_network_levels_api():
    """5 levels deep of the logged-in user's downline, level by level (BFS).
    Each level returns up to 5 real referred people (each hexagon = one
    actual person, never an aggregate) plus the true total at that depth,
    so the UI can show a dull placeholder hexagon for every unfilled slot
    and a highlighted one for every real referral."""
    if "user_id" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT public_user_id FROM users WHERE id = %s", (session["user_id"],))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "not_found"}), 404

            current_ids = [row["public_user_id"]]
            levels = []
            total_pending = 0
            for depth in range(1, 6):
                if not current_ids:
                    levels.append({"depth": depth, "total": 0, "pending": 0, "people": []})
                    continue
                kids_map = _children_map(cur, current_ids)
                flat = []
                for pid in current_ids:
                    flat.extend(kids_map.get(pid, []))
                pending_here = sum(1 for n in flat if n["status"] == "pending")
                total_pending += pending_here
                levels.append({"depth": depth, "total": len(flat), "pending": pending_here, "people": flat[:5]})
                current_ids = [n["id"] for n in flat]

            return jsonify({"levels": levels, "totalPending": total_pending})
    except Exception as e:
        logger.error(f"My-network levels API error: {e}")
        return jsonify({"error": "server_error"}), 500
    finally:
        release_db(conn)

@app.route("/api/my-network")
def my_network_api():
    """Compact version of the network API scoped to the logged-in user's own
    node, for the small live widget on their dashboard (no admin required -
    everyone can see their own referrals, same data already on their dashboard
    table, just visualized)."""
    if "user_id" not in session:
        return jsonify({"error": "not_authenticated"}), 401
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, public_user_id, full_name, status, user_level, referral_count,
                       amount_earned, created_at
                FROM users WHERE id = %s
            """, (session["user_id"],))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "not_found"}), 404
            satellites = _children_map(cur, [row["public_user_id"]]).get(row["public_user_id"], [])
            return jsonify({"center": _node(row, bool(satellites)), "satellites": satellites})
    except Exception as e:
        logger.error(f"My-network API error: {e}")
        return jsonify({"error": "server_error"}), 500
    finally:
        release_db(conn)

# ═════════════════════════════════════════════════════════════
# API
# ═════════════════════════════════════════════════════════════
@app.route("/api/user/check")
def check_user():
    username = request.args.get("username", "").strip()
    email = request.args.get("email", "").strip().lower()

    if not username and not email:
        return jsonify({"exists": False})

    conn = get_db()
    try:
        with conn.cursor() as cur:
            if username and email:
                cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
            elif username:
                cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            else:
                cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            exists = cur.fetchone() is not None
            return jsonify({"exists": exists})
    except Exception as e:
        logger.error(f"Check user error: {e}")
        return jsonify({"exists": False})
    finally:
        release_db(conn)

# ═════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ═════════════════════════════════════════════════════════════
@app.errorhandler(404)
def not_found(e):
    return redirect(url_for("login"))

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Server error: {e}")
    return redirect(url_for("login"))

# ═════════════════════════════════════════════════════════════
# CREATE ADMIN CLI
# ═════════════════════════════════════════════════════════════
@app.cli.command("create-admin")
def create_admin():
    import click
    username = click.prompt("Admin username")
    email = click.prompt("Admin email")
    password = click.prompt("Admin password", hide_input=True)
    full_name = click.prompt("Full name")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            public_id = generate_public_id()
            password_hash = generate_password_hash(password)
            cur.execute("""
                INSERT INTO users (public_user_id, full_name, email, username, password_hash,
                                   status, is_admin, created_at)
                VALUES (%s, %s, %s, %s, %s, 'approved', TRUE, NOW())
                ON CONFLICT (email) DO NOTHING
            """, (public_id, full_name, email, username, password_hash))
            conn.commit()
            click.echo(f"Admin created: {username} (ID: {public_id})")
    except Exception as e:
        logger.error(f"Create admin error: {e}")
        click.echo(f"Error: {e}")
    finally:
        release_db(conn)

# ─── Run ─────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True)
