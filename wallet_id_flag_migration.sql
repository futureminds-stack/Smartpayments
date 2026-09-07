-- ═══════════════════════════════════════════════════════════════
-- Wallet ID becomes genuinely optional at registration: this column
-- tracks whether the current Wallet ID was actually typed by the user,
-- or auto-assigned because they left it blank. Run this in Neon's SQL
-- Editor.
-- ═══════════════════════════════════════════════════════════════

ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_id_set_by_user BOOLEAN NOT NULL DEFAULT FALSE;

-- Existing accounts already went through the "required Wallet ID" flow,
-- so treat their current ID as user-set rather than flagging everyone
-- retroactively. Skip this line if you'd rather start everyone as
-- unflagged.
UPDATE users SET wallet_id_set_by_user = TRUE WHERE public_user_id IS NOT NULL;

-- Let Wallet ID be changed later (via Edit Profile) without breaking
-- referral links: make every foreign key that points at
-- users.public_user_id cascade automatically instead of blocking the
-- update. Looks up the real constraint names instead of assuming them,
-- so this is safe to run regardless of how your schema was created.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT tc.constraint_name, tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND ccu.table_name = 'users'
          AND ccu.column_name = 'public_user_id'
    LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', r.table_name, r.constraint_name);
        EXECUTE format(
            'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES users(public_user_id) ON UPDATE CASCADE %s',
            r.table_name, r.constraint_name, r.column_name,
            CASE WHEN r.table_name = 'users' THEN 'ON DELETE SET NULL' ELSE '' END
        );
    END LOOP;
END $$;
