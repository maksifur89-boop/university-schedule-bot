CREATE SCHEMA IF NOT EXISTS schedule_bot;

CREATE TABLE IF NOT EXISTS schedule_bot.groups (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    number TEXT NOT NULL UNIQUE CHECK (length(number) BETWEEN 1 AND 40),
    university_id TEXT,
    credentials TEXT,
    owner_id BIGINT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    failure_kind TEXT CHECK (failure_kind IN ('account')),
    last_success_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((credentials IS NULL) = (university_id IS NULL))
);

CREATE TABLE IF NOT EXISTS schedule_bot.users (
    telegram_id BIGINT PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES schedule_bot.groups(id),
    login_attempts INTEGER NOT NULL DEFAULT 0,
    login_window TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schedule_bot.login_tickets (
    token_hash TEXT PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES schedule_bot.users(telegram_id),
    group_id BIGINT NOT NULL REFERENCES schedule_bot.groups(id),
    revision INTEGER NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS login_tickets_expiry
    ON schedule_bot.login_tickets (expires_at);
