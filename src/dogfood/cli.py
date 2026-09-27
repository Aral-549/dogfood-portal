"""Operator commands. Run inside the container, e.g.:

    docker compose exec portal python -m dogfood.cli create-admin you@example.org

Needs shell access to the host, which is the point: no web route can create an admin.
"""

import argparse
import secrets
import sys

from . import boot, db, services as svc
from .core.timeutil import format_utc


def create_admin(email: str) -> int:
    email = email.strip().lower()
    if "@" not in email:
        print("not an email address", file=sys.stderr)
        return 2
    from pathlib import Path
    Path(boot.db_path()).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(boot.db_path())
    db.init_schema(conn)
    row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    with db.transaction(conn):
        if row is None:
            uid = "usr_" + secrets.token_hex(6)
            conn.execute("INSERT INTO users (id, email, name, is_admin, created_at) VALUES (?, ?, '', 1, ?)",
                         (uid, email, format_utc(svc.utcnow())))
        else:
            uid = row["id"]
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (uid,))
        svc.audit(conn, "cli", None, "admin.grant", uid, email=email)
    link = svc.create_password_link(conn, uid) if not (row and row["password_hash"]) else None
    print(f"{email} is now an admin.")
    if link:
        print(f"Set a password (valid 7 days): /set-password/{link}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dogfood.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-admin", help="grant admin to an account (created if missing)")
    p.add_argument("email")
    args = ap.parse_args(argv)
    return create_admin(args.email)


if __name__ == "__main__":
    sys.exit(main())
