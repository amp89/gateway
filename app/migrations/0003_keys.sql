CREATE TABLE gateway_keys (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    key_hash TEXT NOT NULL UNIQUE,
    key_hint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'revoked')),
    created_at REAL NOT NULL,
    last_used_at REAL
);
CREATE TABLE permissions (
    id INTEGER PRIMARY KEY,
    key_id INTEGER NOT NULL REFERENCES gateway_keys (id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    scope_json TEXT,
    created_at REAL NOT NULL,
    UNIQUE (key_id, action)
);
