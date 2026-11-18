-- Required fields per operation (JSON list, NULL = none) and the last run error per bot command.
-- commands.last_error_enc is encrypted (it can quote upstream text), NULL = last run was fine or never ran.
ALTER TABLE operations ADD COLUMN required_fields_json TEXT;
ALTER TABLE commands ADD COLUMN last_error_enc TEXT;
ALTER TABLE commands ADD COLUMN last_error_at REAL;
-- Bundled packs installed before this migration: mark their create operations' required fields, as the packs now do.
UPDATE operations SET required_fields_json = '["name", "columnId"]'
 WHERE name = 'tasks_add' AND body_fields_json LIKE '%"name"%' AND body_fields_json LIKE '%"columnId"%'
   AND service_id IN (SELECT id FROM services WHERE source_pack = 'kanbanflow');
UPDATE operations SET required_fields_json = '["content"]'
 WHERE name = 'tasks_add' AND body_fields_json LIKE '%"content"%'
   AND service_id IN (SELECT id FROM services WHERE source_pack = 'todoist');
UPDATE operations SET required_fields_json = '["text"]'
 WHERE name = 'quick_add' AND body_fields_json LIKE '%"text"%'
   AND service_id IN (SELECT id FROM services WHERE source_pack = 'todoist');
UPDATE operations SET required_fields_json = '["title"]'
 WHERE name = 'tasks_add' AND body_fields_json LIKE '%"title"%'
   AND service_id IN (SELECT id FROM services WHERE source_pack = 'vikunja')
