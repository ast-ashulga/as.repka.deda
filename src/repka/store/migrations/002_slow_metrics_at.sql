-- P1-T12: when each slow REST metric was last fetched successfully (NULL = never / last attempt returned nothing).
-- Needed because snapshots carry the last contributors / active_weeks_12 forward, so a snapshot's own
-- age says nothing about how stale the value is.
ALTER TABLE repos ADD COLUMN contributors_at TEXT;
ALTER TABLE repos ADD COLUMN participation_at TEXT;

INSERT INTO schema_version (version) VALUES (2);
