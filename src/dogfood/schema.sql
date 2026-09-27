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
