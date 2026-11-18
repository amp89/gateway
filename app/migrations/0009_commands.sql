CREATE TABLE commands (
    id INTEGER PRIMARY KEY,
    bot_id INTEGER NOT NULL REFERENCES bots (id) ON DELETE CASCADE,
    command TEXT NOT NULL,
    key_id INTEGER NOT NULL REFERENCES gateway_keys (id),
    action TEXT NOT NULL,
    call_enc TEXT NOT NULL,
    reply_enc TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    UNIQUE (bot_id, command)
);
CREATE INDEX commands_bot ON commands (bot_id)
