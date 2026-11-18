CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    bot_id INTEGER NOT NULL REFERENCES bots (id) ON DELETE CASCADE,
    update_id INTEGER NOT NULL,
    tg_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    sent_at REAL NOT NULL,
    received_at REAL NOT NULL,
    sender_enc TEXT NOT NULL,
    body_enc TEXT NOT NULL,
    UNIQUE (bot_id, update_id)
);
CREATE INDEX messages_bot ON messages (bot_id, id);
CREATE INDEX messages_received ON messages (received_at)
