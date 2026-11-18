-- Which bundled pack a service was created from (NULL: made by hand or from an uploaded pack).
ALTER TABLE services ADD COLUMN source_pack TEXT
