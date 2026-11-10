-- Digest insights: a second message per day (the releases message) is its own `digests` row.
-- `kind` is 'main' (every existing row) or 'releases'. A releases row stores its slot as 'releases:HH:MM'
-- (see digest/slots.py slot_key), so the existing UNIQUE (slot, local_date) keeps it apart from a main row
-- without rebuilding a table that `events` and `digest_items` reference.
ALTER TABLE digests ADD COLUMN kind TEXT NOT NULL DEFAULT 'main';

INSERT INTO schema_version (version) VALUES (4);
