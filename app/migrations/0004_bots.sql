CREATE TABLE bots (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    token_enc TEXT NOT NULL,
    allowlist_enc TEXT NOT NULL,
    polling_enabled INTEGER NOT NULL DEFAULT 1,
    poll_offset INTEGER NOT NULL DEFAULT 0,
    poll_error TEXT NOT NULL DEFAULT '',
    retention_days INTEGER NOT NULL DEFAULT 30,
    created_at REAL NOT NULL
);
