"""Leaving a team and rotating its invite link (added 2026-09-27). Over HTTP, real app.

The fixture event closed 2026-03-01; tests reopen submissions through the organizer route.
priya1 (demo participant) is on tm_01 with 2 other members and project prj_01.
"""

import sqlite3

from conftest import ORGANIZER, PARTICIPANT, portal


def _open(c):
    assert c.post("/api/v1/event", json={"submissions_close": "2099-01-01T00:00:00Z"}, headers=ORGANIZER).status_code == 303


def _user(c, email):
    assert c.post("/register", data={"email": email, "password": "a-long-password"}).status_code == 303
    tok = c.post("/api/v1/tokens", json={}).json()["token"]
    c.cookies.clear()
    return {"Authorization": f"Bearer {tok}"}


def _members(data_dir, team):
    conn = sqlite3.connect(str(data_dir / "dogfood.db"))
    try:
        return conn.execute("SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team,)).fetchone()[0]
    finally:
        conn.close()


def test_leave_then_start_another_team(tmp_path):
    with portal(tmp_path) as c:
        _open(c)
        assert _members(tmp_path, "tm_01") == 3
        assert c.post("/api/v1/teams/leave", headers=PARTICIPANT).status_code == 204
        assert _members(tmp_path, "tm_01") == 2
        assert c.post("/api/v1/teams", json={"name": "Fresh start"}, headers=PARTICIPANT).status_code == 303
        assert c.post("/api/v1/teams/leave", headers=PARTICIPANT).status_code == 204   # no projects: ok


def test_leave_refused_when_closed_or_not_in_team(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/teams/leave", headers=PARTICIPANT)
        assert (r.status_code, r.json()["error"]) == (403, "submissions_closed")
        _open(c)
        loner = _user(c, "loner@example.org")
        assert c.post("/api/v1/teams/leave", headers=loner).status_code == 404
        assert c.post("/api/v1/teams/leave").status_code == 401


def test_last_member_with_projects_cannot_leave(tmp_path):
    with portal(tmp_path) as c:
        _open(c)
        solo = _user(c, "solo@example.org")
        assert c.post("/api/v1/teams", json={"name": "Solo"}, headers=solo).status_code == 303
        assert c.post("/api/v1/projects", json={"title": "Mine"}, headers=solo).status_code == 201
        r = c.post("/api/v1/teams/leave", headers=solo)
        assert (r.status_code, r.json()["error"]) == (409, "last_member_with_projects")


def test_rotating_invite_kills_the_old_link(tmp_path):
    with portal(tmp_path) as c:
        _open(c)
        solo = _user(c, "host@example.org")
        c.post("/api/v1/teams", json={"name": "Hosts"}, headers=solo)
        conn = sqlite3.connect(str(tmp_path / "dogfood.db"))
        old = conn.execute("SELECT invite_code FROM teams WHERE name = 'Hosts'").fetchone()[0]
        conn.close()
        new = c.post("/api/v1/teams/invite", headers=solo).json()["invite"]
        assert new != f"/join/{old}"
        guest = _user(c, "guest@example.org")
        assert c.post(f"/join/{old}", headers=guest).status_code == 404
        assert c.post(new, headers=guest).status_code == 303
