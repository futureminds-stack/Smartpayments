-- ═══════════════════════════════════════════════════════════════
-- CREATE ADMIN ACCOUNT
-- Run this in the Neon SQL Editor AFTER running reset_database.sql
-- (or against any DB that already has the users table from schema.sql)
--
-- Creates/updates:
--   Username: Admin_for_BitGlory
--   Email:    admin@bitglory.example   <-- change to your real email
--
-- password_hash is a Werkzeug scrypt hash (not plaintext). Change the
-- password after first login via the forgot-password flow.
-- ═══════════════════════════════════════════════════════════════

INSERT INTO users (
    public_user_id, full_name, email, username, password_hash,
    status, is_admin, created_at
) VALUES (
    'EC2AB53031EF37AF',
    'Admin for BitGlory',
    'admin@bitglory.example',
    'Admin_for_BitGlory',
    'scrypt:32768:8:1$mY9NNWxkpqAAu2pQ$1e8beb659253e329bd731d7be85717b9a998b59dbbed1c240ae73f959c629497490a8c08565edce3bd62b468c4af596e89c380b4c60c647c8e33886092c56830',
    'approved',
    TRUE,
    NOW()
)
ON CONFLICT (username) DO UPDATE SET
    email = EXCLUDED.email,
    password_hash = EXCLUDED.password_hash,
    status = 'approved',
    is_admin = TRUE;

-- Verify:
-- SELECT username, email, status, is_admin FROM users WHERE username = 'Admin_for_BitGlory';
