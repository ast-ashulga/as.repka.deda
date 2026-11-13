-- Initial schema: every table. Embedding vectors live in `embeddings` (no vector-index tables).
-- Timestamps are UTC ISO-8601 strings; JSON columns are TEXT.

CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE domains (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    profile_hash    TEXT,
    scan_every_s    INTEGER,
    last_scan_at    TEXT,
    last_scan_status TEXT
);

CREATE TABLE repos (
    repo_id             INTEGER PRIMARY KEY,
    node_id             TEXT NOT NULL UNIQUE,
    full_name           TEXT NOT NULL,
    html_url            TEXT NOT NULL,
    description         TEXT,
    language            TEXT,
    topics_json         TEXT,
    license             TEXT,
    is_fork             INTEGER NOT NULL DEFAULT 0,
    parent_full_name    TEXT,
    created_at          TEXT,
    pushed_at           TEXT,
    archived            INTEGER NOT NULL DEFAULT 0,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    readme_excerpt      TEXT,
    readme_fetched_at   TEXT
);

CREATE TABLE domain_repos (
    domain_id        TEXT NOT NULL,
    repo_id          INTEGER NOT NULL REFERENCES repos(repo_id),
    first_matched_at TEXT NOT NULL,
    last_matched_at  TEXT NOT NULL,
    query_keys_json  TEXT,
    PRIMARY KEY (domain_id, repo_id)
);

CREATE TABLE snapshots (
    repo_id            INTEGER NOT NULL REFERENCES repos(repo_id),
    taken_at           TEXT NOT NULL,
    stars              INTEGER NOT NULL,
    forks              INTEGER NOT NULL,
    open_issues        INTEGER NOT NULL,
    watchers           INTEGER NOT NULL,
    contributors       INTEGER,
    commits_7d         INTEGER NOT NULL,
    commits_30d        INTEGER NOT NULL,
    active_weeks_12    INTEGER,
    releases_90d       INTEGER NOT NULL,
    latest_release_tag TEXT,
    latest_release_at  TEXT,
    PRIMARY KEY (repo_id, taken_at)
);

CREATE TABLE key_files (
    repo_id       INTEGER NOT NULL REFERENCES repos(repo_id),
    path          TEXT NOT NULL,
    blob_oid      TEXT NOT NULL,
    deps_json     TEXT,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, path, blob_oid)
);

CREATE TABLE events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL REFERENCES repos(repo_id),
    domain_id    TEXT,
    kind         TEXT NOT NULL,
    payload_json TEXT,
    detected_at  TEXT NOT NULL,
    digest_id    INTEGER REFERENCES digests(id)
);

CREATE TABLE verdicts (
    repo_id        INTEGER NOT NULL REFERENCES repos(repo_id),
    domain_id      TEXT NOT NULL,
    model          TEXT NOT NULL,
    questions_hash TEXT NOT NULL,
    input_hash     TEXT NOT NULL,
    answers_json   TEXT,
    accepted       INTEGER NOT NULL DEFAULT 0,
    decided_at     TEXT NOT NULL,
    PRIMARY KEY (repo_id, domain_id, model, questions_hash)
);

CREATE TABLE embeddings (
    repo_id    INTEGER NOT NULL REFERENCES repos(repo_id),
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    vector     BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, model)
);

CREATE TABLE search_cursor (
    domain_id    TEXT NOT NULL,
    query_key    TEXT NOT NULL,
    slice        TEXT NOT NULL,
    completed_at TEXT,
    total_count  INTEGER,
    capped       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (domain_id, query_key, slice)
);

CREATE TABLE http_cache (
    url        TEXT PRIMARY KEY,
    etag       TEXT NOT NULL,
    body       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE digests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slot       TEXT NOT NULL,
    local_date TEXT NOT NULL,
    status     TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    sent_at    TEXT,
    UNIQUE (slot, local_date)
);

CREATE TABLE digest_items (
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    repo_id   INTEGER NOT NULL REFERENCES repos(repo_id),
    kind      TEXT NOT NULL,
    PRIMARY KEY (digest_id, repo_id, kind)
);

-- `kind` records which kind of pass a run was (start_run(conn, kind)).
-- status is NULL while the run is still running.
CREATE TABLE runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status     TEXT,
    stats_json TEXT,
    error      TEXT
);

INSERT INTO schema_version (version) VALUES (1);
