"""Regression cases for BUG-31 .. BUG-35 (BUGLOG.md), derived from the bug symptoms and the edge-case
list in contracts/t3-public.md, not from the implementation's output.

Everything runs over HTTP against the real app (TestClient), except BUG-32, which needs real
concurrency and so boots a real uvicorn process on a free port. No mocks, no monkeypatching.
Windows are set relative to the real clock, as in test_t3_public.py:
  OPEN   = 2026-01-01 .. 2099-01-01
  CLOSED = 2026-01-01 .. 2026-02-01
Fixture facts (fixtures.json): priya1 (demo participant) is on tm_01, which owns prj_01 ("Glass Signal",
track trk_04); prj_02 ("Small Meadow") is tm_02's; prj_03 is tm_03's; prj_07 is superseded by prj_41.
The fixture event's submissions_close (2026-03-01) is in the past, so joining a team or editing a
project first needs submissions_close moved into the future (contracts/lifecycle.md case 22).
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FIXTURES_PATH, ORGANIZER, PARTICIPANT, REPO, SRC, portal

OPEN = {"voting_open": "2026-01-01T00:00:00Z", "voting_close": "2099-01-01T00:00:00Z"}
CLOSED = {"voting_open": "2026-01-01T00:00:00Z", "voting_close": "2026-02-01T00:00:00Z"}
FUTURE_SUBMISSIONS = {"submissions_close": "2099-01-01T00:00:00Z"}


def _window(c, which, **extra):
    r = c.post("/api/v1/event", json={**which, **extra}, headers=ORGANIZER)
    assert r.status_code == 303, r.text


def _new_user(c, email, name="Voter"):
    """Register, mint an API token over the fresh session cookie, then drop the cookie."""
    r = c.post("/register", data={"email": email, "name": name, "password": "a-long-password"})
    assert r.status_code == 303, r.text
    tok = c.post("/api/v1/tokens", json={"name": "t"})
    assert tok.status_code == 201
    c.cookies.clear()
    return {"Authorization": f"Bearer {tok.json()['token']}"}


def _user_id(data_dir, email):
    conn = sqlite3.connect(str(data_dir / "dogfood.db"))
    try:
        return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]
    finally:
        conn.close()


def _invite_code(data_dir, team_id):
    conn = sqlite3.connect(str(data_dir / "dogfood.db"))
    try:
        return conn.execute("SELECT invite_code FROM teams WHERE id = ?", (team_id,)).fetchone()[0]
    finally:
        conn.close()


def _vote(c, headers, project):
    return c.post("/api/v1/votes", json={"project": project}, headers=headers)


def _audit_section(html):
    assert "Audit log (latest 50)" in html
    return html.split("Audit log (latest 50)", 1)[1]


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- BUG-31: dashboard audit log must not reveal who voted for what while voting is open ---------
def test_bug31_dashboard_masks_vote_rows_while_open(tmp_path):
    with portal(tmp_path) as c:
        _window(c, OPEN)
        alice = _new_user(c, "bug31.alice@example.org")
        uid = _user_id(tmp_path, "bug31.alice@example.org")
        assert _vote(c, alice, "prj_02").status_code == 201
        assert c.delete("/api/v1/votes/prj_02", headers=alice).status_code == 204   # vote.withdraw
        assert _vote(c, alice, "prj_02").status_code == 201

        dash = c.get("/organizer", headers=ORGANIZER)
        assert dash.status_code == 200
        audit = _audit_section(dash.text)
        rows = [r for r in audit.split("<tr>") if "vote." in r]
        assert rows, "vote rows are expected in the log, only masked"
        for row in rows:
            assert "prj_02" not in row and "Small Meadow" not in row
            assert uid not in row and "bug31.alice" not in row

        _window(c, CLOSED)
        audit = _audit_section(c.get("/organizer", headers=ORGANIZER).text)
        cast = [r for r in audit.split("<tr>") if "vote.cast" in r]
        assert any("prj_02" in r and uid in r for r in cast)


def test_bug31_vote_void_rows_also_masked_while_open(tmp_path):
    with portal(tmp_path) as c:
        _window(c, OPEN)
        bob = _new_user(c, "bug31.bob@example.org")
        uid = _user_id(tmp_path, "bug31.bob@example.org")
        assert _vote(c, bob, "prj_03").status_code == 201
        assert c.post(f"/api/v1/voters/{uid}/void", json={"reason": "test"}, headers=ORGANIZER).status_code == 303
        audit = _audit_section(c.get("/organizer", headers=ORGANIZER).text)
        for row in (r for r in audit.split("<tr>") if "vote." in r):
            assert "prj_03" not in row


# --- BUG-32: parallel wrong-password logins must not bypass the 5-per-(email, IP) limit -------------
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post_json(base, path, body, timeout=30):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def live_server(tmp_path):
    port = _free_port()
    environ = {k: v for k, v in os.environ.items() if k != "DOGFOOD_RATE_LIMITS"}
    environ.update(DOGFOOD_DATA=str(tmp_path), DOGFOOD_FIXTURES=str(FIXTURES_PATH),
                   PYTHONPATH=str(SRC))
    environ.pop("DOGFOOD_DEMO_SESSIONS", None)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "dogfood.app:app", "--host", "127.0.0.1",
                             "--port", str(port), "--log-level", "warning"],
                            cwd=str(REPO), env=environ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            assert proc.poll() is None, "uvicorn exited during boot"
            assert time.time() < deadline, "uvicorn did not come up"
            time.sleep(0.2)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_bug32_parallel_wrong_passwords_capped_at_five(live_server):
    base = live_server
    assert _post_json(base, "/api/v1/register", {"email": "race@example.org", "name": "R",
                                                 "password": "a-long-password"}) == 201
    n = 20
    barrier = threading.Barrier(n)
    codes = [None] * n

    def guess(i):
        barrier.wait()
        codes[i] = _post_json(base, "/api/v1/login", {"email": "race@example.org", "password": f"wrong-{i}"})

    threads = [threading.Thread(target=guess, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert all(code in (401, 429) for code in codes), codes
    assert 1 <= codes.count(401) <= 5, codes
    assert codes.count(429) == n - codes.count(401)


def test_bug32_success_after_four_failures_is_refunded(live_server):
    base = live_server
    email = "owner@example.org"
    assert _post_json(base, "/api/v1/register", {"email": email, "name": "O", "password": "a-long-password"}) == 201
    assert [_post_json(base, "/api/v1/login", {"email": email, "password": "nope-nope"}) for _ in range(4)] == [401] * 4
    assert _post_json(base, "/api/v1/login", {"email": email, "password": "a-long-password"}) == 200
    # The success neither used up the fifth slot nor left the old failures in place (case 19:
    # a successful login clears that email+IP's count): five fresh failures, then the limit.
    codes = [_post_json(base, "/api/v1/login", {"email": email, "password": "nope-nope"}) for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_bug32_parallel_correct_logins_do_not_consume_the_limit(live_server):
    base = live_server
    email = "busy@example.org"
    assert _post_json(base, "/api/v1/register", {"email": email, "name": "B", "password": "a-long-password"}) == 201
    n = 5
    barrier = threading.Barrier(n)
    codes = [None] * n

    def ok(i):
        barrier.wait()
        codes[i] = _post_json(base, "/api/v1/login", {"email": email, "password": "a-long-password"})

    threads = [threading.Thread(target=ok, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert codes == [200] * n
    assert [_post_json(base, "/api/v1/login", {"email": email, "password": "nope-nope"}) for _ in range(6)] == \
        [401] * 5 + [429]


# --- BUG-33: once a tally has been shown the voting window is final --------------------------------
def test_bug33_close_early_peek_reopen_is_409(tmp_path):
    with portal(tmp_path) as c:
        close = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=2)
        _window(c, {"voting_open": "2026-01-01T00:00:00Z", "voting_close": _iso(close)})
        assert _vote(c, PARTICIPANT, "prj_02").status_code == 201
        while datetime.now(timezone.utc) < close + timedelta(milliseconds=300):
            time.sleep(0.1)
        r = c.get("/api/v1/votes/results")
        assert r.status_code == 200 and r.json() == [{"project": "prj_02", "votes": 1}]
        for change in (OPEN,
                       {"voting_open": "2026-01-01T00:00:00Z", "voting_close": "2099-01-01T00:00:00Z"},
                       {"voting_open": "2026-01-02T00:00:00Z", "voting_close": _iso(close)}):
            r = c.post("/api/v1/event", json=change, headers=ORGANIZER)
            assert (r.status_code, r.json()["error"]) == (409, "voting_closed_final"), change
        r = c.post("/api/v1/event", json={"voting_close": "2099-01-01T00:00:00Z"}, headers=ORGANIZER)
        assert r.status_code == 409
        # still closed, still counted as before
        r = _vote(c, PARTICIPANT, "prj_03")
        assert (r.status_code, r.json()["error"]) == (403, "voting_closed")
        assert c.get("/api/v1/votes/results").json() == [{"project": "prj_02", "votes": 1}]


def test_bug33_form_route_also_final(tmp_path):
    with portal(tmp_path) as c:
        _window(c, CLOSED)
        assert c.get("/api/v1/votes/results").status_code == 200
        r = c.post("/organizer/event", data={"voting_open": "2026-01-01T00:00:00Z",
                                             "voting_close": "2099-01-01T00:00:00Z"}, headers=ORGANIZER)
        assert r.status_code == 409


@pytest.mark.parametrize("reveal", ["vote_page", "dashboard", "export_json"])
def test_bug33_other_reveal_paths_make_window_final(tmp_path, reveal):
    with portal(tmp_path) as c:
        _window(c, CLOSED)
        if reveal == "vote_page":
            assert c.get("/vote", headers=PARTICIPANT).status_code == 200
        elif reveal == "dashboard":
            assert c.get("/organizer", headers=ORGANIZER).status_code == 200
        else:
            assert c.get("/api/v1/events/evt_01/export.json", headers=ORGANIZER).status_code == 200
        r = c.post("/api/v1/event", json=OPEN, headers=ORGANIZER)
        assert (r.status_code, r.json()["error"]) == (409, "voting_closed_final")


def test_bug33_control_reschedule_unseen_window_allowed(tmp_path):
    with portal(tmp_path) as c:
        _window(c, CLOSED)                       # closed, but nobody has looked at a tally
        assert c.get("/api/v1/projects").status_code == 200
        _window(c, OPEN)                         # 303: still allowed
        assert _vote(c, PARTICIPANT, "prj_02").status_code == 201
        r = c.get("/api/v1/votes/results")       # while open: hidden, and hidden does not finalize
        assert (r.status_code, r.json()["error"]) == (403, "results_hidden")
        _window(c, {"voting_open": "2026-01-01T00:00:00Z", "voting_close": "2098-01-01T00:00:00Z"})
        # non-window settings stay editable after a reveal
        _window(c, CLOSED)
        assert c.get("/api/v1/votes/results").status_code == 200
        assert c.post("/api/v1/event", json={"name": "Renamed"}, headers=ORGANIZER).status_code == 303


# --- BUG-34: votes for the voter's own team (joined later) and for dropped projects do not count ------
def test_bug34_vote_then_join_voted_team_does_not_count(tmp_path):
    with portal(tmp_path) as c:
        _window(c, OPEN)
        alice = _new_user(c, "bug34.alice@example.org")
        bob = _new_user(c, "bug34.bob@example.org")
        assert _vote(c, alice, "prj_02").status_code == 201
        assert _vote(c, bob, "prj_02").status_code == 201
        assert _vote(c, PARTICIPANT, "prj_03").status_code == 201
        assert c.post("/organizer/event", json=FUTURE_SUBMISSIONS, headers=ORGANIZER).status_code == 303
        code = _invite_code(tmp_path, "tm_02")
        assert c.post(f"/api/v1/join/{code}", headers=alice).status_code == 303
        _window(c, CLOSED)
        # prj_02: bob only (alice is now on tm_02); prj_03: priya. Tie at 1, ordered by project id.
        assert c.get("/api/v1/votes/results").json() == \
            [{"project": "prj_02", "votes": 1}, {"project": "prj_03", "votes": 1}]


def test_bug34_project_back_to_draft_leaves_tally(tmp_path):
    with portal(tmp_path) as c:
        _window(c, OPEN)
        alice = _new_user(c, "bug34.carol@example.org")
        bob = _new_user(c, "bug34.dave@example.org")
        assert _vote(c, alice, "prj_01").status_code == 201
        assert _vote(c, bob, "prj_01").status_code == 201
        assert _vote(c, PARTICIPANT, "prj_03").status_code == 201
        assert c.post("/organizer/event", json=FUTURE_SUBMISSIONS, headers=ORGANIZER).status_code == 303
        r = c.put("/api/v1/projects/prj_01", json={"title": "Glass Signal", "summary": "One line of what it does.",
                                                   "repo_url": "https://example.org/repo/01", "track_id": "trk_04",
                                                   "draft": True}, headers=PARTICIPANT)
        assert r.status_code == 200, r.text
        _window(c, CLOSED)
        assert c.get("/api/v1/votes/results").json() == [{"project": "prj_03", "votes": 1}]


# --- BUG-35: comment edge cases ---------------------------------------------------------------------
@pytest.mark.parametrize("cid", ["99999999999999999999999", "9223372036854775808", "-1", "0"])
def test_bug35_out_of_range_comment_id_is_404(tmp_path, cid):
    with portal(tmp_path) as c:
        assert c.delete(f"/api/v1/comments/{cid}", headers=PARTICIPANT).status_code == 404
        assert c.delete(f"/api/v1/comments/{cid}", headers=ORGANIZER).status_code == 404
        assert c.post(f"/comments/{cid}/delete", headers=ORGANIZER).status_code == 404


def test_bug35_largest_valid_id_is_404_not_500(tmp_path):
    with portal(tmp_path) as c:
        assert c.delete(f"/api/v1/comments/{2**63 - 1}", headers=ORGANIZER).status_code == 404


def test_bug35_no_comments_on_superseded_or_draft(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/projects/prj_07/comments", json={"body": "dup"}, headers=PARTICIPANT)
        assert r.status_code == 404
        assert c.post("/projects/prj_07/comments", data={"body": "dup"}, headers=PARTICIPANT).status_code == 404
        assert c.post("/api/v1/projects/prj_41/comments", json={"body": "canonical"},
                      headers=PARTICIPANT).status_code == 201


@pytest.mark.parametrize("body", ["hello\x00world", "\x00", "ok\x00"])
def test_bug35_nul_byte_body_is_422(tmp_path, body):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/projects/prj_02/comments", json={"body": body}, headers=PARTICIPANT)
        assert r.status_code == 422
        r = c.post("/projects/prj_02/comments", data={"body": body}, headers=PARTICIPANT)
        assert r.status_code == 422
        conn = sqlite3.connect(str(tmp_path / "dogfood.db"))
        try:
            assert conn.execute("SELECT COUNT(*) FROM comments WHERE instr(body, char(0)) > 0").fetchone()[0] == 0
        finally:
            conn.close()
