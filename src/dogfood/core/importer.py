"""fixtures.json -> database, idempotent. See contracts/fixtures-import.md."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from .. import logs
from .timeutil import format_utc, parse_utc

SCORE_MIN, SCORE_MAX = 1, 5


class FixtureError(ValueError):
    """The file is unusable as a whole; the portal must not start half-seeded."""


@dataclass
class ImportReport:
    counts: dict[str, int] = field(default_factory=dict)
    rejected: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unchanged: bool = False

    def reject(self, entity: str, ref: str, reason: str) -> None:
        self.rejected.append({"entity": entity, "ref": ref, "reason": reason})


def load_file(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise FixtureError(f"{path}: file not found") from e
    except json.JSONDecodeError as e:
        raise FixtureError(f"{path}: not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise FixtureError(f"{path}: top level must be an object")
    return data


def user_id_for(email: str) -> str:
    return "usr_" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]


def invite_code_for(team_id: str, secret: str) -> str:
    return hashlib.sha256(f"{secret}:{team_id}".encode()).hexdigest()[:16]


def _s(value) -> str | None:
    """A usable string reference, or None. Fixture values are untrusted."""
    return value if isinstance(value, str) and value.strip() else None


def _rubric_from(scores: list) -> list[str]:
    """The criteria set used by the most score rows (ties: first seen), in first-seen key order.

    Taking the union instead would let one misspelled or junk row poison every score.
    """
    counts: dict[frozenset, int] = {}
    order: dict[frozenset, list[str]] = {}
    for s in scores:
        crit = s.get("criteria") if isinstance(s, dict) else None
        if isinstance(crit, dict) and crit and all(isinstance(k, str) for k in crit):
            key = frozenset(crit)
            counts[key] = counts.get(key, 0) + 1
            order.setdefault(key, list(crit))
    if not counts:
        return []
    return order[max(counts, key=counts.get)]  # max keeps the first maximum in insertion order


def _list(data: dict, key: str, report: ImportReport) -> list:
    value = data.get(key)
    if value is None:
        report.notes.append(f"no '{key}' in fixtures")
        return []
    if not isinstance(value, list):
        raise FixtureError(f"'{key}' must be a list")
    return value


def _ensure_user(conn, email: str, name: str, now: str) -> str:
    uid = user_id_for(email)
    conn.execute(
        "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = CASE WHEN excluded.name != '' THEN excluded.name ELSE users.name END",
        (uid, email.strip().lower(), name, now),
    )
    return uid


def import_fixtures(conn: sqlite3.Connection, data: dict, now: datetime, invite_secret: str) -> ImportReport:
    """Import in one transaction. Raises FixtureError before touching the db if the file is unusable."""
    report = ImportReport()
    stamp = format_utc(now)
    event = data.get("event")
    if not isinstance(event, dict) or not event.get("id"):
        raise FixtureError("'event' with an 'id' is required")
    try:
        close = format_utc(parse_utc(event.get("submissions_close", "")))
    except ValueError as e:
        raise FixtureError(f"event.submissions_close: {e}") from e
    ev = event["id"]
    tracks, judges, teams = _list(data, "tracks", report), _list(data, "judges", report), _list(data, "teams", report)
    projects, scores = _list(data, "projects", report), _list(data, "scores", report)
    logs.stage("importer", "input", event_id=ev, tracks=len(tracks), judges=len(judges), teams=len(teams),
               projects=len(projects), scores=len(scores))

    before = _fingerprint(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO events (id, name, submissions_close, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, submissions_close = excluded.submissions_close",
            (ev, event.get("name") or ev, close, stamp),
        )
        track_ids = set()
        for t in tracks:
            if not isinstance(t, dict) or not _s(t.get("id")):
                report.reject("track", repr(t)[:60], "missing id")
                continue
            conn.execute("INSERT INTO tracks (id, event_id, name) VALUES (?, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
                         (t["id"], ev, _s(t.get("name")) or t["id"]))
            track_ids.add(t["id"])

        judge_ids = set()
        for j in judges:
            if not isinstance(j, dict) or not _s(j.get("id")) or not _s(j.get("email")):
                report.reject("judge", repr(j)[:60], "missing id or email")
                continue
            uid = _ensure_user(conn, j["email"], _s(j.get("name")) or "", stamp)
            conn.execute("INSERT INTO judges (id, event_id, user_id) VALUES (?, ?, ?) "
                         "ON CONFLICT(id) DO NOTHING", (j["id"], ev, uid))
            judge_tracks = j.get("tracks") if isinstance(j.get("tracks"), list) else []
            for tr in judge_tracks:
                if _s(tr) and tr in track_ids:
                    conn.execute("INSERT OR IGNORE INTO judge_tracks VALUES (?, ?)", (j["id"], tr))
                else:
                    report.reject("judge_track", f"{j['id']}/{tr}", "unknown track")
            judge_ids.add(j["id"])

        team_ids = set()
        for t in teams:
            if not isinstance(t, dict) or not _s(t.get("id")):
                report.reject("team", repr(t)[:60], "missing id")
                continue
            conn.execute("INSERT INTO teams (id, event_id, name, invite_code) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
                         (t["id"], ev, _s(t.get("name")) or t["id"], invite_code_for(t["id"], invite_secret)))
            team_ids.add(t["id"])
            members = t.get("members") if isinstance(t.get("members"), list) else []
            for email in members:
                if not _s(email):
                    report.reject("team_member", f"{t['id']}/{email!r}"[:60], "member is not an email string")
                    continue
                uid = _ensure_user(conn, email, "", stamp)
                existing = conn.execute("SELECT team_id FROM team_members WHERE event_id = ? AND user_id = ?",
                                        (ev, uid)).fetchone()
                if existing and existing["team_id"] != t["id"]:
                    report.reject("team_member", f"{t['id']}/{email}", f"already in {existing['team_id']}")
                    continue
                conn.execute("INSERT OR IGNORE INTO team_members VALUES (?, ?, ?)", (t["id"], uid, ev))

        project_rows = {}
        for p in projects:
            if not isinstance(p, dict) or not _s(p.get("id")) or not _s(p.get("title")):
                report.reject("project", repr(p)[:60], "missing id or title")
                continue
            if not _s(p.get("team")) or p["team"] not in team_ids:
                report.reject("project", p["id"], f"unknown team {p.get('team')!r}")
                continue
            try:
                submitted = format_utc(parse_utc(p.get("submitted_at", "")))
            except ValueError as e:
                report.reject("project", p["id"], f"submitted_at: {e}")
                continue
            track = p["track"] if _s(p.get("track")) and p["track"] in track_ids else None
            conn.execute(
                "INSERT INTO projects (id, event_id, team_id, track_id, title, summary, repo_url, status, "
                "submitted_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET title = excluded.title, summary = excluded.summary, "
                "repo_url = excluded.repo_url, track_id = excluded.track_id, submitted_at = excluded.submitted_at",
                (p["id"], ev, p["team"], track, p["title"], _s(p.get("summary")) or "", _s(p.get("repo_url")) or "",
                 submitted, submitted, submitted),
            )
            project_rows[p["id"]] = {**p, "submitted_at": submitted, "repo_url": _s(p.get("repo_url")) or ""}

        _flag_duplicates(conn, project_rows, report)

        criteria_names = _rubric_from(scores)
        for pos, name in enumerate(criteria_names):
            conn.execute("INSERT OR IGNORE INTO criteria (event_id, name, weight, position) VALUES (?, ?, 1, ?)",
                         (ev, name, pos))
        rubric = [r["name"] for r in conn.execute("SELECT name FROM criteria WHERE event_id = ? ORDER BY position",
                                                  (ev,))]

        seen_pairs = {}
        for idx, s in enumerate(scores):
            ref = f"scores[{idx}]"
            if not isinstance(s, dict):
                report.reject("score", ref, "not an object")
                continue
            judge, project, crit = _s(s.get("judge")), _s(s.get("project")), s.get("criteria")
            if judge is None or judge not in judge_ids:
                report.reject("score", ref, f"unknown judge {s.get('judge')!r}"[:120])
                continue
            if project is None or project not in project_rows:
                report.reject("score", ref, f"unknown project {s.get('project')!r}"[:120])
                continue
            if not isinstance(crit, dict) or set(rubric) - set(crit):
                report.reject("score", ref, "missing rubric criteria")
                continue
            bad = [k for k in rubric if isinstance(crit[k], bool) or not isinstance(crit[k], int)
                   or not SCORE_MIN <= crit[k] <= SCORE_MAX]
            if bad:
                report.reject("score", ref, f"value out of range for {bad}")
                continue
            if (judge, project) in seen_pairs:
                report.notes.append(f"{ref} replaces {seen_pairs[(judge, project)]} for {judge}/{project}")
            seen_pairs[(judge, project)] = ref
            conn.execute("INSERT OR IGNORE INTO assignments VALUES (?, ?)", (judge, project))
            row = conn.execute(
                "INSERT INTO reviews (judge_id, project_id, comment, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(judge_id, project_id) DO UPDATE SET comment = excluded.comment RETURNING id",
                (judge, project, s.get("comment") if isinstance(s.get("comment"), str) else "", stamp),
            ).fetchone()
            conn.execute("DELETE FROM review_scores WHERE review_id = ?", (row["id"],))
            conn.executemany("INSERT INTO review_scores VALUES (?, ?, ?)",
                             [(row["id"], k, crit[k]) for k in rubric])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    report.counts = {
        "events": 1, "tracks": len(track_ids), "judges": len(judge_ids), "teams": len(team_ids),
        "projects": len(project_rows), "scores": len(seen_pairs), "rejected": len(report.rejected),
    }
    report.unchanged = before is not None and before == _fingerprint(conn)
    logs.stage("importer", "output", counts=report.counts, rejected=report.rejected,
               duplicates=report.duplicates, notes=report.notes, unchanged=report.unchanged)
    return report


def _flag_duplicates(conn, project_rows: dict, report: ImportReport) -> None:
    """Same team AND (same case-folded title OR same repo_url): latest submission is canonical."""
    by_team: dict[str, list[dict]] = {}
    for p in project_rows.values():
        by_team.setdefault(p["team"], []).append(p)
    for team, ps in by_team.items():
        ps.sort(key=lambda p: (p["submitted_at"], p["id"]))
        for i, older in enumerate(ps):
            for newer in ps[i + 1:]:
                same_title = older["title"].strip().casefold() == newer["title"].strip().casefold()
                same_repo = bool(older.get("repo_url")) and older.get("repo_url") == newer.get("repo_url")
                if same_title or same_repo:
                    conn.execute("UPDATE projects SET superseded_by = ? WHERE id = ?", (newer["id"], older["id"]))
                    report.duplicates.append({"superseded": older["id"], "canonical": newer["id"], "team": team})
                    break


def _fingerprint(conn) -> str | None:
    """Content hash of imported tables, to report 'unchanged' on re-import."""
    parts = []
    for table in ("events", "tracks", "judges", "judge_tracks", "teams", "team_members", "projects",
                  "criteria", "assignments", "review_scores"):
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        parts.append(repr([tuple(r) for r in rows]))
    reviews = conn.execute("SELECT judge_id, project_id, comment FROM reviews ORDER BY 1, 2").fetchall()
    parts.append(repr([tuple(r) for r in reviews]))
    joined = "\n".join(parts)
    return None if not joined.strip("[]\n") else hashlib.sha256(joined.encode()).hexdigest()
