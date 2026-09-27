"""Startup: schema, fixture import, demo accounts. Prints the checker's auth headers."""

import os
import secrets
import sqlite3
import sys
from pathlib import Path

from . import db, logs, services as svc
from .core import importer
from .core.timeutil import format_utc

DEMO_PASSWORD = "dogfood-demo"
DEMO_ORGANIZER_EMAIL = "organizer@dogfood.local"
# Fixed so .dogfood.toml can hold them. Only created when DOGFOOD_DEMO_SESSIONS=1.
DEMO_ACCOUNTS = {
    "organizer": ("demo-organizer", DEMO_ORGANIZER_EMAIL),
    "judge_a": ("demo-judge-a", "jdg_24"),
    "judge_b": ("demo-judge-b", "jdg_26"),
    "participant": ("demo-participant", "priya1@example.org"),
}


def _secret(data_dir: Path) -> str:
    path = data_dir / "secret"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()


def boot() -> sqlite3.Connection:
    data_dir = Path(os.environ.get("DOGFOOD_DATA", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(data_dir / "dogfood.db"))
    db.init_schema(conn)
    fixtures = os.environ.get("DOGFOOD_FIXTURES", "fixtures.json")
    now = svc.utcnow()
    data = importer.load_file(fixtures)  # FixtureError aborts startup: never half-seeded
    event_id = data["event"]["id"] if isinstance(data.get("event"), dict) else None
    if event_id and svc.get_event(conn, event_id) is None:
        report = importer.import_fixtures(conn, data, now, _secret(data_dir))
        svc.audit(conn, "system", event_id, "fixtures.import", fixtures, counts=report.counts,
                  duplicates=report.duplicates, rejected=report.rejected)
    else:
        logs.stage("importer", "skip", reason="event already present", event_id=event_id)
    if os.environ.get("DOGFOOD_DEMO_SESSIONS") == "1":
        _demo_accounts(conn, event_id)
    else:
        conn.execute("DELETE FROM sessions WHERE label = 'demo'")
    return conn


def _demo_accounts(conn: sqlite3.Connection, event_id: str) -> None:
    stamp = format_utc(svc.utcnow())
    org_id = "usr_demo_organizer"
    conn.execute("INSERT OR IGNORE INTO users (id, email, name, is_admin, created_at) VALUES (?, ?, ?, 1, ?)",
                 (org_id, DEMO_ORGANIZER_EMAIL, "Demo Organizer", stamp))
    conn.execute("INSERT OR IGNORE INTO organizers VALUES (?, ?)", (event_id, org_id))
    lines = []
    for role, (token, ref) in DEMO_ACCOUNTS.items():
        if ref.startswith("jdg_"):
            row = conn.execute("SELECT user_id FROM judges WHERE id = ?", (ref,)).fetchone()
        else:
            row = conn.execute("SELECT id AS user_id FROM users WHERE email = ?", (ref,)).fetchone()
        if row is None:
            logs.stage("boot", "demo_account_missing", role=role, ref=ref)
            continue
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (svc.hash_password(DEMO_PASSWORD), row["user_id"]))
        svc.create_session(conn, row["user_id"], label="demo", token=token, ttl=None)
        email = conn.execute("SELECT email FROM users WHERE id = ?", (row["user_id"],)).fetchone()["email"]
        lines.append(f'{role:<11} = "Authorization: Bearer {token}"   # login: {email} / {DEMO_PASSWORD}')
    banner = "\n".join(["", "DEMO MODE: auth headers for .dogfood.toml [auth]", *lines, ""])
    print(banner, file=sys.stderr, flush=True)
