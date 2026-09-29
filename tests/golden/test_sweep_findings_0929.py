"""Findings of the 2026-09-29 adversarial sweep (not yet in BUGLOG.md). Over HTTP, real app, no mocks.

A reason that starts with a NUL byte passes the Python "reason required" check (clean() only strips
whitespace) but fails SQLite's CHECK (length(trim(reason)) > 0), because SQLite's length() stops at
the first NUL. Where the INSERT is plain this surfaces as a 500; where it is INSERT OR IGNORE the row
is silently dropped while the route reports success.

Fixture facts: jdg_24 (demo judge A) has a counted review of prj_06; jdg_26 is demo judge B.
"""

import sqlite3

import pytest

from conftest import JUDGE_A, ORGANIZER, portal


def _db(data_dir):
    return sqlite3.connect(str(data_dir / "dogfood.db"))


@pytest.mark.parametrize("reason", ["\x00", "\x00former colleague"])
def test_exclude_judge_nul_reason_is_not_500(tmp_path, reason):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/judges/jdg_26/exclude", json={"reason": reason}, headers=ORGANIZER)
        assert r.status_code in (303, 422), r.status_code
        r = c.post("/organizer/judges/jdg_26/exclude", data={"reason": reason}, headers=ORGANIZER)
        assert r.status_code in (303, 422), r.status_code


@pytest.mark.parametrize("reason", ["\x00", "\x00sock puppet"])
def test_void_voter_nul_reason_is_not_500(tmp_path, reason):
    with portal(tmp_path) as c:
        assert c.post("/register", data={"email": "nul.void@example.org", "name": "V",
                                          "password": "a-long-password"}).status_code == 303
        c.cookies.clear()
        conn = _db(tmp_path)
        uid = conn.execute("SELECT id FROM users WHERE email = 'nul.void@example.org'").fetchone()[0]
        conn.close()
        r = c.post(f"/api/v1/voters/{uid}/void", json={"reason": reason}, headers=ORGANIZER)
        assert r.status_code in (303, 422), r.status_code


@pytest.mark.parametrize("reason", ["\x00", "\x00former colleague"])
def test_recusal_nul_reason_is_rejected_or_recorded(tmp_path, reason):
    """Never 'recused' in the response and the audit log while the recusal row is missing and the
    judge's review keeps counting."""
    with portal(tmp_path) as c:
        r = c.post("/api/v1/judge/recusals/prj_06", json={"reason": reason}, headers=JUDGE_A)
        conn = _db(tmp_path)
        try:
            recorded = conn.execute("SELECT COUNT(*) FROM recusals WHERE judge_id = 'jdg_24' "
                                    "AND project_id = 'prj_06'").fetchone()[0]
        finally:
            conn.close()
        assert r.status_code == 422 or (r.status_code == 204 and recorded == 1), (r.status_code, recorded)
