-- P2-T03: cached 2-D projection + clusters for the dashboard landscape map.
-- payload is JSON; params_hash covers the projected vectors' content and the parameters, so a changed
-- vector set or parameter makes the row stale. One row per (domain, model, method).
CREATE TABLE landscape_cache (
    domain_id   TEXT NOT NULL,
    model       TEXT NOT NULL,
    method      TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (domain_id, model, method)
);

INSERT INTO schema_version (version) VALUES (3);
