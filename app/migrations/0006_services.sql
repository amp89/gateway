CREATE TABLE services (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    auth_style TEXT NOT NULL CHECK (auth_style IN ('bearer', 'header', 'query')),
    auth_name TEXT NOT NULL DEFAULT '',
    credential_enc TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
CREATE TABLE operations (
    id INTEGER PRIMARY KEY,
    service_id INTEGER NOT NULL REFERENCES services (id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')),
    path_template TEXT NOT NULL,
    query_fields_json TEXT,
    body_fields_json TEXT,
    scope_fields_json TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE (service_id, name)
)
