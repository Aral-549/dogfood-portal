"""Database-facing helpers shared by the web layer. Authorization lives in core.authz."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import logs
from .core import scoring
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
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


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


def results(conn, event_id: str) -> tuple[list[scoring.ProjectResult], dict[str, dict]]:
    """Compute the ranking for an event. Returns (results, project metadata)."""
    projects = conn.execute(
        "SELECT p.id, p.title, p.superseded_by, t.name AS team, COALESCE(tr.name, '') AS track "
        "FROM projects p JOIN teams t ON t.id = p.team_id LEFT JOIN tracks tr ON tr.id = p.track_id "
        "WHERE p.event_id = ? AND p.status = 'submitted' ORDER BY p.id", (event_id,)).fetchall()
    superseded = {p["id"]: p["superseded_by"] for p in projects if p["superseded_by"]}
    canonical = [p["id"] for p in projects if not p["superseded_by"]]
    reviews: dict[int, scoring.Review] = {}
    rows = conn.execute(
        "SELECT r.id, r.judge_id, r.project_id, s.criterion, s.value FROM reviews r "
        "JOIN projects p ON p.id = r.project_id JOIN review_scores s ON s.review_id = r.id "
        "WHERE p.event_id = ?", (event_id,)).fetchall()
    grouped: dict[int, tuple[str, str, dict]] = {}
    for r in rows:
        grouped.setdefault(r["id"], (r["judge_id"], r["project_id"], {}))[2][r["criterion"]] = r["value"]
    for rid, (j, p, c) in grouped.items():
        reviews[rid] = scoring.Review(j, p, c)
    weights = rubric(conn, event_id)
    effective = scoring.effective_reviews(reviews.values(), superseded)
    complete = [r for r in effective if set(weights) <= set(r.criteria)]
    dropped = {"merged_duplicates": len(reviews) - len(effective), "incomplete": len(effective) - len(complete)}
    if any(dropped.values()):
        logs.stage("scoring", "reviews_not_counted", event_id=event_id, **dropped)
    res = scoring.score(weights, complete, canonical) if weights else []
    meta = {p["id"]: {"title": p["title"], "team": p["team"], "track": p["track"],
                      "superseded_by": p["superseded_by"]} for p in projects}
    logs.stage("scoring", "output", event_id=event_id, projects=len(canonical), reviews=len(complete),
               superseded=superseded)
    return res, meta


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
