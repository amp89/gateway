CREATE TABLE audit (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    key_id INTEGER,
    key_name TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    client_ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX audit_ts ON audit (ts);
CREATE INDEX audit_key ON audit (key_id, ts);
