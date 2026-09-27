"""Database-facing helpers shared by the web layer. Authorization lives in core.authz."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import logs
from .core import agreement, confidence, scoring
from .core.authz import Actor
from .core.timeutil import format_utc, parse_utc

PAGE_SIZE = 50
MAX_PAGE = 10_000
MAX_TEAM_SIZE = 4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- passwords and sessions --------------------------------------------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password, stored: str | None) -> bool:
    if not isinstance(password, str):
        password = ""
    if not stored or not stored.startswith("scrypt$"):
        # Burn comparable time so unknown users are not distinguishable by timing.
        hashlib.scrypt(password.encode(), salt=b"0" * 16, n=2**14, r=8, p=1)
        return False
    _, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(candidate.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, user_id: str, label: str = "login", token: str | None = None,
                   ttl: timedelta | None = timedelta(days=14)) -> str:
    token = token or secrets.token_urlsafe(32)
    now = utcnow()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, label, created_at, expires_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(token_hash) DO UPDATE SET user_id = excluded.user_id, expires_at = excluded.expires_at",
        (_token_hash(token), user_id, label, format_utc(now), format_utc(now + ttl) if ttl else None),
    )
    return token


def delete_session(conn, token: str) -> None:
    """Revoke a login session. Demo sessions are shared fixtures and only boot removes them."""
    conn.execute("DELETE FROM sessions WHERE token_hash = ? AND label != 'demo'", (_token_hash(token),))


def load_actor(conn, token: str | None, now: datetime) -> Actor:
    if not token:
        return Actor()
    row = conn.execute("SELECT s.user_id, s.expires_at, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id "
                       "WHERE s.token_hash = ?", (_token_hash(token),)).fetchone()
    if row is None or (row["expires_at"] and parse_utc(row["expires_at"]) <= now):
        return Actor()
    uid = row["user_id"]
    organizer_of = frozenset(r[0] for r in conn.execute("SELECT event_id FROM organizers WHERE user_id = ?", (uid,)))
    judge_of = {r[0]: r[1] for r in conn.execute("SELECT event_id, id FROM judges WHERE user_id = ?", (uid,))}
    team_of = {r[0]: r[1] for r in conn.execute("SELECT event_id, team_id FROM team_members WHERE user_id = ?", (uid,))}
    return Actor(user_id=uid, is_admin=bool(row["is_admin"]), organizer_of=organizer_of,
                 judge_of=judge_of, team_of=team_of)


def create_password_link(conn, user_id: str) -> str | None:
    """One-time link for an account that has NO password yet. Never a reset of someone else's."""
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["password_hash"]:
        return None
    # The organizer carries these links by hand (no email). A participant's account must never be
    # claimable by the organizer, so team members never get one (BUG-23).
    if conn.execute("SELECT 1 FROM team_members WHERE user_id = ?", (user_id,)).fetchone():
        return None
    token = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO password_links VALUES (?, ?, ?)",
                 (_token_hash(token), user_id, format_utc(utcnow() + timedelta(days=7))))
    return token


def consume_password_link(conn, token: str, password: str) -> str | None:
    row = conn.execute("SELECT user_id, expires_at FROM password_links WHERE token_hash = ?",
                       (_token_hash(token),)).fetchone()
    if row is None or parse_utc(row["expires_at"]) <= utcnow():
        return None
    conn.execute("DELETE FROM password_links WHERE token_hash = ?", (_token_hash(token),))
    # Re-checked on use, so a link issued before this rule existed cannot claim a participant (BUG-28).
    if conn.execute("SELECT 1 FROM team_members WHERE user_id = ?", (row["user_id"],)).fetchone():
        return None
    # Only sets a first password: a link can never overwrite one the user already chose.
    updated = conn.execute("UPDATE users SET password_hash = ? WHERE id = ? AND password_hash IS NULL",
                           (hash_password(password), row["user_id"])).rowcount
    if not updated:
        return None
    conn.execute("DELETE FROM sessions WHERE user_id = ? AND label != 'demo'", (row["user_id"],))
    return row["user_id"]


# --- audit -------------------------------------------------------------------

def audit(conn, actor: Actor | str, event_id: str | None, action: str, subject: str = "", **detail) -> None:
    who = actor if isinstance(actor, str) else (actor.user_id or "visitor")
    conn.execute("INSERT INTO audit_log (at, actor, event_id, action, subject, detail) VALUES (?, ?, ?, ?, ?, ?)",
                 (format_utc(utcnow()), who, event_id, action, subject, json.dumps(detail, default=str)))
    logs.stage("audit", action, actor=who, event_id=event_id, subject=subject)


# --- events --------------------------------------------------------------------

def default_event_id(conn) -> str | None:
    row = conn.execute("SELECT id FROM events ORDER BY created_at, id LIMIT 1").fetchone()
    return row["id"] if row else None


def get_event(conn, event_id: str | None):
    if not event_id:
        return None
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def event_close(event) -> datetime:
    return parse_utc(event["submissions_close"])


def judging_open(event, now: datetime) -> bool:
    return not event["judging_close"] or now < parse_utc(event["judging_close"])


# --- gallery -------------------------------------------------------------------

def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def gallery(conn, event_id: str, q: str = "", track: str = "", page: int = 1):
    sql = ("SELECT p.id, p.title, p.summary, p.repo_url, p.submitted_at, t.name AS team, "
           "tr.id AS track_id, tr.name AS track FROM projects p JOIN teams t ON t.id = p.team_id "
           "LEFT JOIN tracks tr ON tr.id = p.track_id "
           "WHERE p.event_id = ? AND p.status = 'submitted' AND p.superseded_by IS NULL")
    args: list = [event_id]
    if q:
        like = f"%{_like_escape(q)}%"
        sql += " AND (p.title LIKE ? ESCAPE '\\' OR p.summary LIKE ? ESCAPE '\\')"
        args += [like, like]
    if track:
        sql += " AND p.track_id = ?"
        args.append(track)
    total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
    rows = conn.execute(sql + " ORDER BY p.id LIMIT ? OFFSET ?", args + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    return [dict(r) for r in rows], total


# --- scores and results ------------------------------------------------------------

def judge_scores(conn, judge_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT r.id, r.project_id, p.title, r.comment, r.updated_at, s.criterion, s.value "
        "FROM reviews r JOIN projects p ON p.id = r.project_id JOIN review_scores s ON s.review_id = r.id "
        "WHERE r.judge_id = ? ORDER BY r.project_id, s.criterion", (judge_id,)).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        item = out.setdefault(r["id"], {"judge": judge_id, "project": r["project_id"], "title": r["title"],
                                        "comment": r["comment"], "updated_at": r["updated_at"], "criteria": {}})
        item["criteria"][r["criterion"]] = r["value"]
    return list(out.values())


def rubric(conn, event_id: str) -> dict[str, float]:
    return {r["name"]: r["weight"] for r in
            conn.execute("SELECT name, weight FROM criteria WHERE event_id = ? ORDER BY position, name", (event_id,))}


def _scoring_inputs(conn, event_id: str):
    """(weights, effective complete reviews, canonical project ids, project metadata, excluded judges)."""
    projects = conn.execute(
        "SELECT p.id, p.title, p.superseded_by, p.track_id, t.name AS team, COALESCE(tr.name, '') AS track "
        "FROM projects p JOIN teams t ON t.id = p.team_id LEFT JOIN tracks tr ON tr.id = p.track_id "
        "WHERE p.event_id = ? AND p.status = 'submitted' ORDER BY p.id", (event_id,)).fetchall()
    superseded = {p["id"]: p["superseded_by"] for p in projects if p["superseded_by"]}
    canonical = [p["id"] for p in projects if not p["superseded_by"]]
    rows = conn.execute(
        "SELECT r.id, r.judge_id, r.project_id, s.criterion, s.value FROM reviews r "
        "JOIN projects p ON p.id = r.project_id JOIN review_scores s ON s.review_id = r.id "
        "WHERE p.event_id = ?", (event_id,)).fetchall()
    grouped: dict[int, tuple[str, str, dict]] = {}
    for r in rows:
        grouped.setdefault(r["id"], (r["judge_id"], r["project_id"], {}))[2][r["criterion"]] = r["value"]
    reviews = [scoring.Review(j, p, c) for j, p, c in grouped.values()]
    weights = rubric(conn, event_id)
    effective = scoring.effective_reviews(reviews, superseded)
    complete = [r for r in effective if set(weights) <= set(r.criteria)]
    dropped = {"merged_duplicates": len(reviews) - len(effective), "incomplete": len(effective) - len(complete)}
    if any(dropped.values()):
        logs.stage("scoring", "reviews_not_counted", event_id=event_id, **dropped)
    # Conflict of interest: a judge who is (now) on a project's team never counts for it,
    # even for a review given before joining (scoring re-checks too, see BUG-12).
    conflicted = {(r["jid"], r["pid"]) for r in conn.execute(
        "SELECT j.id AS jid, p.id AS pid FROM judges j JOIN team_members m ON m.user_id = j.user_id "
        "JOIN projects p ON p.team_id = m.team_id WHERE j.event_id = ?", (event_id,))}
    if conflicted:
        before = len(complete)
        complete = [r for r in complete if (r.judge, r.project) not in conflicted
                    and (r.judge, superseded.get(r.project, r.project)) not in conflicted]
        if len(complete) != before:
            logs.stage("scoring", "conflicted_reviews_not_counted", event_id=event_id, count=before - len(complete))
    excluded = {r["judge_id"]: dict(r) for r in conn.execute(
        "SELECT x.* FROM judge_exclusions x JOIN judges j ON j.id = x.judge_id WHERE j.event_id = ?", (event_id,))}
    meta = {p["id"]: {"title": p["title"], "team": p["team"], "track": p["track"], "track_id": p["track_id"],
                      "superseded_by": p["superseded_by"]} for p in projects}
    return weights, complete, canonical, meta, excluded


def results(conn, event_id: str) -> tuple[list[scoring.ProjectResult], dict[str, dict]]:
    """The published ranking: organizer-excluded judges are left out."""
    weights, reviews, canonical, meta, excluded = _scoring_inputs(conn, event_id)
    counted = [r for r in reviews if r.judge not in excluded]
    res = scoring.score(weights, counted, canonical) if weights else []
    logs.stage("scoring", "output", event_id=event_id, projects=len(canonical), reviews=len(counted),
               excluded_judges=sorted(excluded))
    return res, meta


# Pure results keyed by their exact inputs (all frozen dataclasses): the dashboard and the CSVs
# are reloaded far more often than scores change, and the Monte Carlo run dominates each load.
_ANALYSIS_CACHE: dict[str, tuple] = {}
_ANALYSIS_LOCK = threading.Lock()


def analysis(conn, event_id: str, k: int) -> dict:
    """Ranking plus prize-line confidence (same reviews as the ranking) and judge agreement.

    Agreement is computed over ALL reviews, excluded judges included, so the organizer can
    see the evidence behind an exclusion and undo it.
    """
    weights, reviews, canonical, meta, excluded = _scoring_inputs(conn, event_id)
    if not weights:
        return {"results": [], "meta": meta, "confidence": None, "agreement": [], "favoritism": [],
                "excluded": excluded}
    key = repr((event_id, sorted(weights.items()), reviews, canonical, sorted(excluded), k))
    cached = _ANALYSIS_CACHE.get(key)
    if cached is None:
        counted = [r for r in reviews if r.judge not in excluded]
        res = scoring.score(weights, counted, canonical)
        scored, _ = scoring.score_reviews(weights, counted, canonical)
        conf = confidence.prize_confidence(scored, res, k)
        all_scored, informative = scoring.score_reviews(weights, reviews, canonical)
        agreement_rows, flags = agreement.judge_agreement(all_scored, informative)
        cached = (res, conf, agreement_rows, flags)
        with _ANALYSIS_LOCK:
            _ANALYSIS_CACHE[key] = cached
            while len(_ANALYSIS_CACHE) > 32:
                _ANALYSIS_CACHE.pop(next(iter(_ANALYSIS_CACHE)))
    res, conf, agreement_rows, flags = cached
    logs.stage("analysis", "output", event_id=event_id, k=k, method=conf.method,
               close_calls=[p for p, c in conf.projects.items() if c.close_call],
               outliers=[a.judge for a in agreement_rows if a.status == "outlier"], favoritism=len(flags))
    return {"results": res, "meta": meta, "confidence": conf, "agreement": agreement_rows,
            "favoritism": flags, "excluded": excluded}


def progress(conn, event_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT j.id, u.name, u.email, "
        "(SELECT COUNT(*) FROM assignments a WHERE a.judge_id = j.id) AS assigned, "
        "(SELECT COUNT(*) FROM reviews r WHERE r.judge_id = j.id) AS done "
        "FROM judges j JOIN users u ON u.id = j.user_id WHERE j.event_id = ? ORDER BY j.id", (event_id,)).fetchall()
    return [dict(r) for r in rows]


# --- teams ---------------------------------------------------------------------------

def team_size(conn, team_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team_id,)).fetchone()[0]


# --- T4: tokens, bulk export/import ---------------------------------------------------------------

def token_id(token: str) -> str:
    """Public id of an API token: a prefix of its hash, never the token."""
    return _token_hash(token)[:16]


def export_event(conn, ev, voting_closed: bool) -> dict:
    """fixtures.json shape (so core/importer reads it back) plus extras. No secrets, no hashes, no tokens."""
    eid = ev["id"]
    tracks = [{"id": r["id"], "name": r["name"]} for r in
              conn.execute("SELECT id, name FROM tracks WHERE event_id = ? ORDER BY id", (eid,))]
    judges = []
    for j in conn.execute("SELECT j.id, u.name, u.email FROM judges j JOIN users u ON u.id = j.user_id "
                          "WHERE j.event_id = ? ORDER BY j.id", (eid,)):
        tr = [r[0] for r in conn.execute("SELECT track_id FROM judge_tracks WHERE judge_id = ? ORDER BY track_id",
                                         (j["id"],))]
        judges.append({"id": j["id"], "name": j["name"], "email": j["email"], "tracks": tr})
    teams = []
    for t in conn.execute("SELECT id, name FROM teams WHERE event_id = ? ORDER BY id", (eid,)):
        members = [r[0] for r in conn.execute("SELECT u.email FROM team_members m JOIN users u ON u.id = m.user_id "
                                              "WHERE m.team_id = ? ORDER BY u.email", (t["id"],))]
        teams.append({"id": t["id"], "name": t["name"], "members": members})
    projects = [{"id": p["id"], "team": p["team_id"], "track": p["track_id"], "title": p["title"],
                 "summary": p["summary"], "repo_url": p["repo_url"], "submitted_at": p["submitted_at"]}
                for p in conn.execute("SELECT * FROM projects WHERE event_id = ? AND status = 'submitted' "
                                      "ORDER BY id", (eid,))]
    scores = []
    for r in conn.execute("SELECT r.id, r.judge_id, r.project_id, r.comment FROM reviews r JOIN projects p "
                          "ON p.id = r.project_id WHERE p.event_id = ? ORDER BY r.id", (eid,)):
        crit = {c["criterion"]: c["value"] for c in
                conn.execute("SELECT criterion, value FROM review_scores WHERE review_id = ?", (r["id"],))}
        scores.append({"judge": r["judge_id"], "project": r["project_id"], "criteria": crit, "comment": r["comment"]})
    out = {
        "event": {"id": eid, "name": ev["name"], "submissions_close": ev["submissions_close"],
                  "judging_close": ev["judging_close"], "voting_open": ev["voting_open"],
                  "voting_close": ev["voting_close"], "votes_per_voter": ev["votes_per_voter"],
                  "prizes": ev["prizes"]},
        "tracks": tracks, "judges": judges, "teams": teams, "projects": projects, "scores": scores,
        "rubric": [{"name": r["name"], "weight": r["weight"]} for r in
                   conn.execute("SELECT name, weight FROM criteria WHERE event_id = ? ORDER BY position, name", (eid,))],
        "exclusions": [dict(r) for r in conn.execute(
            "SELECT x.judge_id, x.reason, x.at FROM judge_exclusions x JOIN judges j ON j.id = x.judge_id "
            "WHERE j.event_id = ?", (eid,))],
        "comments": [dict(r) for r in conn.execute(
            "SELECT c.project_id AS project, u.email AS author, c.body, c.created_at FROM comments c "
            "JOIN users u ON u.id = c.user_id JOIN projects p ON p.id = c.project_id "
            "WHERE p.event_id = ? AND c.deleted_at IS NULL ORDER BY c.id", (eid,))],
        # Beyond the fixtures shape, so a move mid-event loses nothing: pending assignments,
        # drafts, co-organizers, voided voters.
        "assignments": [{"judge": r[0], "project": r[1]} for r in conn.execute(
            "SELECT a.judge_id, a.project_id FROM assignments a JOIN judges j ON j.id = a.judge_id "
            "WHERE j.event_id = ? ORDER BY 1, 2", (eid,))],
        "drafts": [{"id": p["id"], "team": p["team_id"], "track": p["track_id"], "title": p["title"],
                    "summary": p["summary"], "repo_url": p["repo_url"]}
                   for p in conn.execute("SELECT * FROM projects WHERE event_id = ? AND status = 'draft' "
                                         "ORDER BY id", (eid,))],
        "organizers": [r[0] for r in conn.execute(
            "SELECT u.email FROM organizers o JOIN users u ON u.id = o.user_id WHERE o.event_id = ? "
            "ORDER BY u.email", (eid,))],
        "voided_voters": [dict(r) for r in conn.execute(
            "SELECT u.email AS voter, v.reason, v.at FROM voided_voters v JOIN users u ON u.id = v.user_id "
            "WHERE v.event_id = ? ORDER BY u.email", (eid,))],
    }
    if voting_closed:  # tallies stay hidden until the window closes, exports included
        out["votes"] = [dict(r) for r in conn.execute(
            "SELECT u.email AS voter, v.project_id AS project, v.at FROM votes v JOIN users u ON u.id = v.user_id "
            "WHERE v.event_id = ? ORDER BY v.at", (eid,))]
    return out


def _list_of(data: dict, key: str) -> list:
    value = data.get(key)
    return value if isinstance(value, list) else []


def _import_people_and_activity(conn, event_id: str, data: dict, stamp: str) -> None:
    """Assignments, drafts, organizers, comments, votes, voided voters. Rows that do not fit the
    event are skipped (like the importer's rejected rows), never written half-way."""
    from .core.importer import _ensure_user
    judges = {r[0] for r in conn.execute("SELECT id FROM judges WHERE event_id = ?", (event_id,))}
    teams = {r[0] for r in conn.execute("SELECT id FROM teams WHERE event_id = ?", (event_id,))}
    tracks = {r[0] for r in conn.execute("SELECT id FROM tracks WHERE event_id = ?", (event_id,))}
    for d in _list_of(data, "drafts"):
        if isinstance(d, dict) and isinstance(d.get("id"), str) and d["id"].strip() and d.get("team") in teams \
                and isinstance(d.get("title"), str) and d["title"].strip():
            conn.execute("INSERT OR IGNORE INTO projects (id, event_id, team_id, track_id, title, summary, repo_url, "
                         "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
                         (d["id"], event_id, d["team"], d.get("track") if d.get("track") in tracks else None,
                          d["title"][:200], str(d.get("summary") or "")[:2000], str(d.get("repo_url") or "")[:2000],
                          stamp, stamp))
    projects = {r[0] for r in conn.execute("SELECT id FROM projects WHERE event_id = ?", (event_id,))}
    for a in _list_of(data, "assignments"):
        if isinstance(a, dict) and a.get("judge") in judges and a.get("project") in projects:
            conn.execute("INSERT OR IGNORE INTO assignments VALUES (?, ?)", (a["judge"], a["project"]))

    def user(email):
        return _ensure_user(conn, email, "", stamp) if isinstance(email, str) and "@" in email else None

    for email in _list_of(data, "organizers"):
        if (uid := user(email)):
            conn.execute("INSERT OR IGNORE INTO organizers VALUES (?, ?)", (event_id, uid))
    for c in _list_of(data, "comments"):
        if isinstance(c, dict) and c.get("project") in projects and isinstance(c.get("body"), str) \
                and 1 <= len(c["body"].strip()) <= 2000 and (uid := user(c.get("author"))):
            conn.execute("INSERT INTO comments (project_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
                         (c["project"], uid, c["body"].strip(), str(c.get("created_at") or stamp)[:40]))
    for v in _list_of(data, "votes"):
        if isinstance(v, dict) and v.get("project") in projects and (uid := user(v.get("voter"))):
            conn.execute("INSERT OR IGNORE INTO votes VALUES (?, ?, ?, ?)",
                         (event_id, uid, v["project"], str(v.get("at") or stamp)[:40]))
    for v in _list_of(data, "voided_voters"):
        if isinstance(v, dict) and isinstance(v.get("reason"), str) and v["reason"].strip() \
                and (uid := user(v.get("voter"))):
            conn.execute("INSERT OR IGNORE INTO voided_voters VALUES (?, ?, ?, ?, ?)",
                         (event_id, uid, v["reason"][:1000], "import", str(v.get("at") or stamp)[:40]))


def apply_import_extras(conn, event_id: str, data: dict, actor: Actor, now: datetime) -> None:
    """Rubric weights, exclusions, event settings and the importing admin as organizer."""
    from . import db as _db  # local import: db imports nothing from here, avoids a cycle at module load
    ev = data.get("event") or {}
    stamp = format_utc(now)
    with _db.transaction(conn):
        conn.execute("INSERT OR IGNORE INTO organizers VALUES (?, ?)", (event_id, actor.user_id))
        for key in ("judging_close", "voting_open", "voting_close"):
            if isinstance(ev.get(key), str) and ev[key]:
                try:
                    conn.execute(f"UPDATE events SET {key} = ? WHERE id = ?",  # key from a fixed tuple
                                 (format_utc(parse_utc(ev[key])), event_id))
                except ValueError:
                    pass
        if isinstance(ev.get("prizes"), str):
            conn.execute("UPDATE events SET prizes = ? WHERE id = ?", (ev["prizes"][:2000], event_id))
        vpv = ev.get("votes_per_voter")
        if isinstance(vpv, int) and not isinstance(vpv, bool) and 1 <= vpv <= 100:
            conn.execute("UPDATE events SET votes_per_voter = ? WHERE id = ?", (vpv, event_id))
        # The importer derives the rubric from score rows; an event exported before any scoring
        # has none, so its rubric comes from here (criteria only added while no reviews exist).
        has_reviews = conn.execute("SELECT 1 FROM reviews r JOIN judges j ON j.id = r.judge_id "
                                   "WHERE j.event_id = ? LIMIT 1", (event_id,)).fetchone()
        for pos, c in enumerate(_list_of(data, "rubric")):
            if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip():
                w = c.get("weight")
                w = float(w) if isinstance(w, (int, float)) and not isinstance(w, bool) and 0 < w <= 1000 else 1.0
                if not has_reviews:
                    conn.execute("INSERT OR IGNORE INTO criteria (event_id, name, weight, position) VALUES (?, ?, ?, ?)",
                                 (event_id, c["name"].strip()[:100], w, pos))
                conn.execute("UPDATE criteria SET weight = ?, position = ? WHERE event_id = ? AND name = ?",
                             (w, pos, event_id, c["name"]))
        _import_people_and_activity(conn, event_id, data, stamp)
        for x in data.get("exclusions") or []:
            if isinstance(x, dict) and isinstance(x.get("judge_id"), str) and isinstance(x.get("reason"), str) \
                    and x["reason"].strip() and conn.execute(
                        "SELECT 1 FROM judges WHERE id = ? AND event_id = ?", (x["judge_id"], event_id)).fetchone():
                conn.execute("INSERT OR IGNORE INTO judge_exclusions VALUES (?, ?, ?, ?)",
                             (x["judge_id"], x["reason"][:1000], actor.user_id, stamp))
        audit(conn, actor, event_id, "event.import", event_id)
