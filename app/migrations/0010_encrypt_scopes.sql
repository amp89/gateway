-- Permission scopes (which name bots, chat IDs, project IDs) encrypted at rest, like 0008 did elsewhere.
-- scope_enc set = limited (encrypted JSON); both NULL = no limit. Old scope_json rows are converted on the next
-- unlock (Gateway.backfill_encrypted), after which scope_json is always NULL.
ALTER TABLE permissions ADD COLUMN scope_enc TEXT
