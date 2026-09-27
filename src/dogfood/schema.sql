-- DOGFOOD portal schema. SQLite. See DATA-MODEL.md for the rationale.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT,                       -- NULL: cannot log in with a password
    is_admin      INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,             -- sha256 of the token; tokens are never stored
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    expires_at  TEXT                          -- NULL: until revoked
);

CREATE TABLE IF NOT EXISTS events (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    submissions_close TEXT NOT NULL,
    judging_close     TEXT,
    results_published INTEGER NOT NULL DEFAULT 0 CHECK (results_published IN (0, 1)),
    prizes            TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organizers (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, user_id)
);

CREATE TABLE IF NOT EXISTS tracks (
    id       TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS judges (
    id       TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE (event_id, user_id)
);

CREATE TABLE IF NOT EXISTS judge_tracks (
    judge_id TEXT NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    PRIMARY KEY (judge_id, track_id)
);

CREATE TABLE IF NOT EXISTS teams (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    invite_code TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id  TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    PRIMARY KEY (team_id, user_id),
    UNIQUE (event_id, user_id)                -- one team per person per event
);

CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    event_id      TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    team_id       TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    track_id      TEXT REFERENCES tracks(id),
    title         TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    repo_url      TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL CHECK (status IN ('draft', 'submitted')),
    submitted_at  TEXT,
    superseded_by TEXT REFERENCES projects(id),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS criteria (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    weight   REAL NOT NULL CHECK (weight > 0),
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (event_id, name)
);

CREATE TABLE IF NOT EXISTS assignments (
    judge_id   TEXT NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    PRIMARY KEY (judge_id, project_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY,
    judge_id   TEXT NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    comment    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE (judge_id, project_id)
);

CREATE TABLE IF NOT EXISTS review_scores (
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    criterion TEXT NOT NULL,
    value     INTEGER NOT NULL CHECK (value BETWEEN 1 AND 5),
    PRIMARY KEY (review_id, criterion)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY,
    at       TEXT NOT NULL,
    actor    TEXT,                            -- user id, or 'system'
    event_id TEXT,
    action   TEXT NOT NULL,
    subject  TEXT NOT NULL DEFAULT '',
    detail   TEXT NOT NULL DEFAULT '{}'       -- JSON
);

CREATE INDEX IF NOT EXISTS idx_projects_event ON projects(event_id, status);
CREATE INDEX IF NOT EXISTS idx_reviews_judge ON reviews(judge_id);
CREATE INDEX IF NOT EXISTS idx_reviews_project ON reviews(project_id);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event_id, at);

CREATE TABLE IF NOT EXISTS password_links (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);

-- Accounts given a known password by demo mode; cleared when demo mode is off.
CREATE TABLE IF NOT EXISTS demo_accounts (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE
);

-- Organizer decision to leave a judge's reviews out of results. Reversible; reviews are never deleted.
CREATE TABLE IF NOT EXISTS judge_exclusions (
    judge_id    TEXT PRIMARY KEY REFERENCES judges(id) ON DELETE CASCADE,
    reason      TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    excluded_by TEXT NOT NULL,
    at          TEXT NOT NULL
);

-- Extra judges added to settle close calls at the prize line (contracts/confidence.md).
CREATE TABLE IF NOT EXISTS tiebreak_assignments (
    judge_id   TEXT NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    at         TEXT NOT NULL,
    PRIMARY KEY (judge_id, project_id)
);

-- T3 public participation (contracts/t3-public.md). Event voting columns are added by db.migrate().
CREATE TABLE IF NOT EXISTS votes (
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    at         TEXT NOT NULL,
    PRIMARY KEY (user_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_votes_event ON votes(event_id, user_id);

CREATE TABLE IF NOT EXISTS voided_voters (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reason   TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    by_user  TEXT NOT NULL,
    at       TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body       TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
    created_at TEXT NOT NULL,
    deleted_at TEXT                             -- soft delete: the audit trail keeps what happened
);
CREATE INDEX IF NOT EXISTS idx_comments_project ON comments(project_id, created_at);

CREATE TABLE IF NOT EXISTS abuse_flags (
    id       INTEGER PRIMARY KEY,
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,                     -- duplicate_email | many_accounts_one_ip
    detail   TEXT NOT NULL DEFAULT '{}',
    at       TEXT NOT NULL
);

-- T4 (contracts/t4-*.md)
CREATE TABLE IF NOT EXISTS records (
    id         TEXT PRIMARY KEY,                -- 128 random bits, hex
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('participant', 'judge')),
    payload    TEXT NOT NULL,                   -- canonical JSON that was signed
    signature  TEXT NOT NULL,                   -- base64 Ed25519
    issued_at  TEXT NOT NULL,
    UNIQUE (event_id, user_id, kind)
);

CREATE TABLE IF NOT EXISTS webhooks (
    id         TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    secret     TEXT NOT NULL,                   -- needed to sign; never returned by the API
    events     TEXT NOT NULL,                   -- JSON list
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id          TEXT PRIMARY KEY,
    webhook_id  TEXT NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    event       TEXT NOT NULL,
    body        TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_status INTEGER,
    last_error  TEXT,
    next_at     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_due ON webhook_deliveries(state, next_at);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    delivery_id TEXT NOT NULL REFERENCES webhook_deliveries(id) ON DELETE CASCADE,
    attempt     INTEGER NOT NULL,
    at          TEXT NOT NULL,
    status      INTEGER,
    error       TEXT,
    PRIMARY KEY (delivery_id, attempt)
);
