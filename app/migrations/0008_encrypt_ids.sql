-- Telegram chat IDs and audit resources (which name bots, chat IDs, project IDs) encrypted at rest.
-- Old plaintext rows are converted on the next unlock (Gateway.backfill_encrypted); new rows keep chat_id = 0.
-- audit.resource_enc: NULL = row from before this migration, '' = written while sealed (resource stays plain).
ALTER TABLE messages ADD COLUMN chat_id_enc TEXT;
ALTER TABLE audit ADD COLUMN resource_enc TEXT
