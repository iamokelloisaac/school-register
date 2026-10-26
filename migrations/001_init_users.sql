-- 001_init_users.sql
-- Creates: citext extension, user_role enum, users table.

CREATE EXTENSION IF NOT EXISTS citext;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
        CREATE TYPE user_role AS ENUM ('student', 'admin');
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL PRIMARY KEY,
    email           CITEXT NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,
    full_name       TEXT NOT NULL,
    age             INT NOT NULL CHECK (age >= 0 AND age <= 150),
    role            user_role NOT NULL DEFAULT 'student',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);