"""contracts/t3-public.md: community voting, comments, hidden results, ballot order, anti-abuse.

Window rules and ballot order are checked as pure functions with injected times; everything
else runs over HTTP against the real app. The organizer opens or closes the window through
the settings route, relative to the real clock:
  OPEN   = 2026-01-01 .. 2099-01-01 (contains any real "now")
  CLOSED = 2026-01-01 .. 2026-02-01 (entirely in the past)
Audit rows are read straight from the sqlite file with the stdlib.

Fixture facts (fixtures.json): priya1 (demo participant) is on tm_01, which owns prj_01;
prj_02 is tm_02's, prj_03 tm_03's; prj_07 is superseded by prj_41 (fixtures-import.md case 6);
the demo judge is jdg_24. 41 project rows - 1 superseded = 40 ballot entries.
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from conftest import EVENT_ID, FIXTURES_PATH, JUDGE_A, ORGANIZER, PARTICIPANT, env, load_fixture_json, portal
from dogfood.core import authz
from dogfood.core.authz import Actor, Decision
from dogfood.core.public import ballot_order, normalize_email, tally, voting_state
from dogfood.core.ratelimit import SlidingWindow

OPEN = {"voting_open": "2026-01-01T00:00:00Z", "voting_close": "2099-01-01T00:00:00Z"}
CLOSED = {"voting_open": "2026-01-01T00:00:00Z", "voting_close": "2026-02-01T00:00:00Z"}
V_OPEN = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
V_CLOSE = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)


def _canonical_ids():
    d = load_fixture_json()
    return sorted(p["id"] for p in d["projects"] if p["id"] != "prj_07")


def _audit(data_dir, action):
    conn = sqlite3.connect(str(data_dir / "dogfood.db"))
    try:
        return conn.execute("SELECT actor, subject, detail FROM audit_log WHERE action = ? ORDER BY id",
                            (action,)).fetchall()
    finally:
        conn.close()


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


def _vote(c, headers, project):
    return c.post("/api/v1/votes", json={"project": project}, headers=headers)


@pytest.fixture
def open_portal(tmp_path):
    with portal(tmp_path) as c:
        _window(c, OPEN)
        yield c, tmp_path


# --- window rule (pure, injected time) ----------------------------------------------------
def test_window_half_open():
    o, cl = "2026-04-01T12:00:00Z", "2026-04-02T12:00:00Z"
    assert voting_state(o, cl, V_OPEN - S) == "before"
    assert voting_state(o, cl, V_OPEN) == "open"
    assert voting_state(o, cl, V_CLOSE - S) == "open"
    assert voting_state(o, cl, V_CLOSE) == "closed"            # case 8: exactly at close
    assert voting_state(o, cl, V_CLOSE + S) == "closed"
    assert voting_state(None, None, V_OPEN) == "unset"
    assert voting_state(o, None, V_OPEN) == "unset"


def test_case8_authz_before_and_at_close_is_voting_closed():
    voter = Actor(user_id="u1", team_of={EVENT_ID: "tm_01"})
    for state in ("before", "closed", "unset"):
        assert authz.cast_vote(voter, EVENT_ID, state, "tm_02") == (Decision.FORBIDDEN, "voting_closed")
    assert authz.cast_vote(voter, EVENT_ID, "open", "tm_02") == (Decision.ALLOW, None)


def test_case10_results_hidden_rule_is_role_blind():
    for state in ("unset", "before", "open"):
        assert authz.read_vote_results(state) == (Decision.FORBIDDEN, "results_hidden")
    assert authz.read_vote_results("closed") == (Decision.ALLOW, None)


# --- case 13/14: ballot order ------------------------------------------------------------
def test_case13_order_is_sha256_of_event_user_project():
    ids = _canonical_ids()
    expected = sorted(ids, key=lambda p: hashlib.sha256(f"{EVENT_ID}:u_alice:{p}".encode()).hexdigest())
    assert ballot_order(EVENT_ID, "u_alice", ids) == expected
    assert ballot_order(EVENT_ID, "u_alice", list(reversed(ids))) == expected   # input order irrelevant
    assert sorted(ballot_order(EVENT_ID, "u_alice", ids)) == ids              # each exactly once


def test_case14_two_voters_get_different_orders():
    ids = _canonical_ids()
    assert len(ids) == 40
    assert ballot_order(EVENT_ID, "u_alice", ids) != ballot_order(EVENT_ID, "u_bob", ids)


def test_case13_ballot_over_http_stable_and_complete(open_portal):
    c, _ = open_portal
    first = c.get("/api/v1/ballot", headers=PARTICIPANT).json()
    again = c.get("/api/v1/ballot", headers=PARTICIPANT).json()
    ids = [p["id"] for p in first["projects"]]
    assert ids == [p["id"] for p in again["projects"]]
    assert sorted(ids) == _canonical_ids()
    assert "prj_07" not in ids and "prj_41" in ids
    other = _new_user(c, "ballot.other@example.org")
    assert [p["id"] for p in c.get("/api/v1/ballot", headers=other).json()["projects"]] != ids


# --- cases 1-9: casting ------------------------------------------------------------------
def test_case1_visitor_401(open_portal):
    c, _ = open_portal
    assert c.post("/api/v1/votes", json={"project": "prj_02"}).status_code == 401


def test_case2_to_5_cast_duplicate_limit_withdraw(open_portal):
    c, data = open_portal
    r = _vote(c, PARTICIPANT, "prj_02")
    assert r.status_code == 201                                            # case 2
    assert [row[1] for row in _audit(data, "vote.cast")] == ["prj_02"]
    r = _vote(c, PARTICIPANT, "prj_02")
    assert (r.status_code, r.json()["error"]) == (409, "already_voted")    # case 3
    r = _vote(c, PARTICIPANT, "prj_03")
    assert (r.status_code, r.json()["error"]) == (409, "vote_limit")       # case 4 (default 1 vote)
    assert c.delete("/api/v1/votes/prj_02", headers=PARTICIPANT).status_code == 204   # case 5
    assert [row[1] for row in _audit(data, "vote.withdraw")] == ["prj_02"]
    assert _vote(c, PARTICIPANT, "prj_03").status_code == 201
    b = c.get("/api/v1/ballot", headers=PARTICIPANT).json()
    assert [p["id"] for p in b["projects"] if p["voted"]] == ["prj_03"] and b["votes_left"] == 0


def test_votes_per_voter_three(tmp_path):
    with portal(tmp_path) as c:
        _window(c, OPEN, votes_per_voter="3")
        assert [_vote(c, PARTICIPANT, p).status_code for p in ("prj_02", "prj_03", "prj_04", "prj_05")] == \
            [201, 201, 201, 409]


def test_case6_own_team_conflict(open_portal):
    c, _ = open_portal
    r = _vote(c, PARTICIPANT, "prj_01")
    assert (r.status_code, r.json()["error"]) == (403, "conflict_of_interest")


def test_case7_judge_does_not_vote(open_portal):
    c, _ = open_portal
    r = _vote(c, JUDGE_A, "prj_02")
    assert (r.status_code, r.json()["error"]) == (403, "judges_do_not_vote")


def test_case8_closed_and_before_over_http(tmp_path):
    with portal(tmp_path) as c:
        _window(c, CLOSED)
        r = _vote(c, PARTICIPANT, "prj_02")
        assert (r.status_code, r.json()["error"]) == (403, "voting_closed")
        _window(c, {"voting_open": "2098-01-01T00:00:00Z", "voting_close": "2099-01-01T00:00:00Z"})
        r = _vote(c, PARTICIPANT, "prj_02")
        assert (r.status_code, r.json()["error"]) == (403, "voting_closed")


def test_case9_superseded_or_unknown_not_on_ballot(open_portal):
    c, _ = open_portal
    for pid in ("prj_07", "prj_nope"):
        r = _vote(c, PARTICIPANT, pid)
        assert (r.status_code, r.json()["error"]) == (404, "not_on_ballot")


# --- cases 10-12, 22: hidden tallies, results, voiding -----------------------------------
def test_case10_to_12_and_22_tallies(tmp_path):
    with portal(tmp_path) as c:
        _window(c, OPEN)
        alice = _new_user(c, "alice.voter@example.org")
        bob = _new_user(c, "bob.voter@example.org")
        assert _vote(c, PARTICIPANT, "prj_03").status_code == 201
        assert _vote(c, alice, "prj_02").status_code == 201
        assert _vote(c, bob, "prj_02").status_code == 201
        for h in ({}, PARTICIPANT, JUDGE_A, ORGANIZER):                     # case 10
            r = c.get("/api/v1/votes/results", headers=h)
            assert (r.status_code, r.json()["error"]) == (403, "results_hidden")
        # case 12: no per-project counts anywhere while open
        for item in c.get("/api/v1/projects").json()["items"]:
            assert not any("vote" in k for k in item)
        assert "vote" not in c.get("/api/v1/export.csv", headers=ORGANIZER).text.splitlines()[0].lower()
        dash = c.get("/organizer", headers=ORGANIZER).text
        assert "3 votes cast" in dash

        _window(c, CLOSED)
        r = c.get("/api/v1/votes/results")                                  # case 11, even for visitors
        assert r.status_code == 200
        assert r.json() == [{"project": "prj_02", "votes": 2}, {"project": "prj_03", "votes": 1}]

        # case 22: void one prj_02 voter (a reason is required), then restore
        uid = next(a for a, s, _ in _audit(tmp_path, "vote.cast") if s == "prj_02")
        assert c.post(f"/api/v1/voters/{uid}/void", json={"reason": " "}, headers=ORGANIZER).status_code == 422
        assert c.post(f"/api/v1/voters/{uid}/void", json={"reason": "sock puppet"},
                      headers=ORGANIZER).status_code == 303
        assert [row[1] for row in _audit(tmp_path, "vote.void")] == [uid]
        assert c.get("/api/v1/votes/results").json() == \
            [{"project": "prj_02", "votes": 1}, {"project": "prj_03", "votes": 1}]
        assert c.post(f"/api/v1/voters/{uid}/unvoid", headers=ORGANIZER).status_code == 303
        assert c.get("/api/v1/votes/results").json()[0] == {"project": "prj_02", "votes": 2}


def test_void_is_organizer_only(open_portal):
    c, _ = open_portal
    r = c.post("/api/v1/voters/whoever/void", json={"reason": "x"}, headers=PARTICIPANT)
    assert r.status_code == 403


def test_tally_ignores_voided_and_orders_by_votes_then_id():
    votes = [("a", "prj_09"), ("b", "prj_09"), ("c", "prj_03"), ("d", "prj_02"), ("x", "prj_02"), ("x", "prj_05")]
    assert tally(votes, {"x"}) == [{"project": "prj_09", "votes": 2}, {"project": "prj_02", "votes": 1},
                                   {"project": "prj_03", "votes": 1}]


# --- cases 15-17: comments -----------------------------------------------------------------
def test_case15_comment_created_and_escaped(tmp_path):
    with portal(tmp_path) as c:
        body = "<script>alert(1)</script> nice"
        r = c.post("/api/v1/projects/prj_02/comments", json={"body": body}, headers=PARTICIPANT)
        assert r.status_code == 201 and r.json()["body"] == body
        page = c.get("/projects/prj_02").text
        assert "&lt;script&gt;alert(1)&lt;/script&gt; nice" in page and "<script>alert(1)" not in page
        assert len(_audit(tmp_path, "comment.create")) == 1


def test_case16_visitor_empty_too_long(tmp_path):
    with portal(tmp_path) as c:
        url = "/api/v1/projects/prj_02/comments"
        assert c.post(url, json={"body": "hi"}).status_code == 401
        assert c.post(url, json={"body": "   "}, headers=PARTICIPANT).status_code == 422
        assert c.post(url, json={"body": "x" * 2001}, headers=PARTICIPANT).status_code == 422
        assert c.post(url, json={"body": "x" * 2000}, headers=PARTICIPANT).status_code == 201


def test_case17_delete_by_author_organizer_other(tmp_path):
    with portal(tmp_path) as c:
        other = _new_user(c, "other.commenter@example.org")
        url = "/api/v1/projects/prj_02/comments"
        c1 = c.post(url, json={"body": "one"}, headers=PARTICIPANT).json()["id"]
        c2 = c.post(url, json={"body": "two"}, headers=PARTICIPANT).json()["id"]
        assert c.delete(f"/api/v1/comments/{c1}", headers=other).status_code == 403
        assert c.delete(f"/api/v1/comments/{c1}", headers=PARTICIPANT).status_code == 204
        assert c.delete(f"/api/v1/comments/{c2}", headers=ORGANIZER).status_code == 204
        assert [row[1] for row in _audit(tmp_path, "comment.delete")] == [str(c1), str(c2)]
        assert "one" not in c.get("/projects/prj_02").text.split('id="comments"', 1)[1]


# --- cases 18-21: abuse --------------------------------------------------------------------
def test_sliding_window_unit():
    w = SlidingWindow(3, 60)
    assert [w.hit("k", t)[0] for t in (0, 1, 2, 3)] == [True, True, True, False]
    ok, retry = w.hit("k", 30)
    assert not ok and retry == pytest.approx(30)       # oldest hit (t=0) leaves at t=60
    assert w.hit("k", 60)[0] is True                   # window is (now-60, now]
    assert w.hit("other", 3)[0] is True                # per key


def test_case18_votes_and_withdrawals_rate_limited(open_portal):
    c, _ = open_portal
    codes = []
    for i in range(5):
        codes.append(_vote(c, PARTICIPANT, "prj_02").status_code)
        codes.append(c.delete("/api/v1/votes/prj_02", headers=PARTICIPANT).status_code)
    assert codes == [201, 204] * 5
    r = _vote(c, PARTICIPANT, "prj_02")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_case18_comments_rate_limited(tmp_path):
    with portal(tmp_path) as c:
        url = "/api/v1/projects/prj_02/comments"
        codes = [c.post(url, json={"body": f"c{i}"}, headers=PARTICIPANT).status_code for i in range(6)]
        assert codes == [201] * 5 + [429]


def test_case19_failed_logins_per_email(tmp_path):
    with portal(tmp_path) as c:
        _new_user(c, "lock.me@example.org")
        bad = {"email": "lock.me@example.org", "password": "wrong-password"}
        assert [c.post("/login", data=bad).status_code for _ in range(5)] == [401] * 5
        r = c.post("/login", data={"email": "lock.me@example.org", "password": "a-long-password"})
        assert r.status_code == 429 and "Retry-After" in r.headers   # even the right password
        # other emails are unaffected
        _new_user(c, "fine.user@example.org")
        assert c.post("/login", data={"email": "fine.user@example.org",
                                      "password": "a-long-password"}).status_code == 303


def test_case20_many_accounts_one_ip_flagged_not_blocked(tmp_path):
    with portal(tmp_path) as c:
        for i in range(5):
            _new_user(c, f"nat{i}@campus.example.org")
        flags = [json.loads(d).get("kind") for _, _, d in _audit(tmp_path, "abuse.flag")]
        assert flags == ["many_accounts_one_ip", "many_accounts_one_ip"]   # 4th and 5th


@pytest.mark.parametrize("a, b, same", [
    ("a.b+x@gmail.com", "ab@gmail.com", True),
    ("A.B@GoogleMail.com", "ab@gmail.com", True),
    ("a.b@example.org", "ab@example.org", False),     # dots only matter to gmail
    ("a+x@example.org", "a@example.org", False),
])
def test_case21_normalize_email(a, b, same):
    assert (normalize_email(a) == normalize_email(b)) is same


def test_case21_duplicate_email_flagged(tmp_path):
    with portal(tmp_path) as c:
        _new_user(c, "ab@gmail.com")
        _new_user(c, "a.b+x@gmail.com")
        kinds = [json.loads(d).get("kind") for _, _, d in _audit(tmp_path, "abuse.flag")]
        assert kinds == ["duplicate_email"]
        assert "a.b+x@gmail.com" in c.get("/organizer", headers=ORGANIZER).text   # case 22: listed


# --- edge cases ------------------------------------------------------------------------------
def test_edge_unset_window_page_explains(tmp_path):
    with portal(tmp_path) as c:
        r = c.get("/vote", headers=PARTICIPANT)
        assert r.status_code == 200 and "not been set up" in r.text


def test_edge_close_before_open_is_422(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/event", json={"voting_open": "2026-05-01T00:00:00Z",
                                          "voting_close": "2026-04-01T00:00:00Z"}, headers=ORGANIZER)
        assert r.status_code == 422
        r = c.post("/api/v1/event", json={"voting_open": "2026-05-01T00:00:00Z"}, headers=ORGANIZER)
        assert r.status_code == 422                                        # both or neither


# --- improvements beyond the contract (2026-09-27) ---------------------------------------------
# Failed logins are throttled per (email, IP) at 5, per email at 20 and per IP at 50, so an attacker
# cannot lock an account's owner out from elsewhere. The client IP is set by a test-only ASGI wrapper
# (X-Test-IP header -> scope["client"]); the app itself is untouched.
@contextmanager
def ip_portal(data_dir):
    from fastapi.testclient import TestClient
    from dogfood.app import app

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            ip = dict(scope["headers"]).get(b"x-test-ip")
            if ip:
                scope = {**scope, "client": (ip.decode(), 50000)}
        await app(scope, receive, send)

    with env(DOGFOOD_DATA=str(data_dir), DOGFOOD_FIXTURES=str(FIXTURES_PATH), DOGFOOD_DEMO_SESSIONS="1"):
        with TestClient(wrapped, follow_redirects=False, raise_server_exceptions=False) as client:
            yield client


def _login(c, ip, email, password):
    return c.post("/login", data={"email": email, "password": password}, headers={"X-Test-IP": ip}).status_code


def test_lockout_from_one_ip_does_not_lock_out_the_owner(tmp_path):
    with ip_portal(tmp_path) as c:
        _new_user(c, "victim@example.org")
        assert [_login(c, "6.6.6.6", "victim@example.org", "guess") for _ in range(6)] == [401] * 5 + [429]
        assert _login(c, "10.0.0.1", "victim@example.org", "a-long-password") == 303


def test_per_email_ceiling_across_many_ips(tmp_path):
    with ip_portal(tmp_path) as c:
        _new_user(c, "target@example.org")
        codes = [_login(c, f"7.7.{i}.1", "target@example.org", "guess") for i in range(21)]
        assert codes == [401] * 20 + [429]
        assert _login(c, "10.0.0.1", "target@example.org", "a-long-password") == 429


def test_per_ip_ceiling_against_password_spraying(tmp_path):
    with ip_portal(tmp_path) as c:
        codes = [_login(c, "8.8.8.8", f"nobody{i}@example.org", "Summer2026!") for i in range(51)]
        assert codes == [401] * 50 + [429]
        assert _login(c, "9.9.9.9", "nobody0@example.org", "x") == 401          # other IPs unaffected


def test_success_forgives_own_typos(tmp_path):
    with ip_portal(tmp_path) as c:
        _new_user(c, "typo@example.org")
        for _ in range(3):
            assert [_login(c, "1.1.1.1", "typo@example.org", "oops") for _ in range(4)] == [401] * 4
            assert _login(c, "1.1.1.1", "typo@example.org", "a-long-password") == 303


def test_browser_gets_a_page_on_429(tmp_path):
    with portal(tmp_path) as c:
        _new_user(c, "html@example.org")
        for _ in range(5):
            c.post("/login", data={"email": "html@example.org", "password": "no"})
        r = c.post("/login", data={"email": "html@example.org", "password": "no"}, headers={"Accept": "text/html"})
        assert r.status_code == 429 and "text/html" in r.headers["content-type"] and "Retry-After" in r.headers


def test_rate_limits_off_switch():
    from dogfood.app import rate_limits
    off = rate_limits("off")
    assert all(off["login_fail"].hit("k", 0)[0] for _ in range(1000))
    assert off["register_ip"].enabled                     # flags are never switched off
    assert rate_limits("on")["login_fail"].enabled


def test_limiter_forgets_expired_keys():
    w = SlidingWindow(1, 10)
    for i in range(5000):
        w.hit(f"random{i}@example.org", float(i))
    assert len(w) < 1100                                    # swept, not 5000


def test_new_judge_votes_stop_counting(tmp_path):
    with portal(tmp_path) as c:
        _window(c, OPEN)
        voter = _new_user(c, "late.judge@example.org")
        assert _vote(c, voter, "prj_02").status_code == 201
        assert c.post("/api/v1/judges", json={"email": "late.judge@example.org"}, headers=ORGANIZER).status_code == 303
        _window(c, CLOSED)
        assert c.get("/api/v1/votes/results").json() == []


def test_moved_close_resets_voting_closed_webhook(tmp_path):
    with portal(tmp_path) as c:
        _window(c, CLOSED)
    conn = sqlite3.connect(str(tmp_path / "dogfood.db"))
    conn.execute("UPDATE events SET voting_closed_sent = 1")
    conn.commit()
    conn.close()
    with portal(tmp_path) as c:
        _window(c, OPEN)                                      # extended into the future
        _window(c, OPEN)                                      # unchanged close: no reset needed
    conn = sqlite3.connect(str(tmp_path / "dogfood.db"))
    assert conn.execute("SELECT voting_closed_sent FROM events WHERE id = ?", (EVENT_ID,)).fetchone()[0] == 0
    conn.close()


def test_duplicate_email_uses_indexed_column(tmp_path):
    with portal(tmp_path) as c:
        _new_user(c, "first.last@gmail.com")
        _new_user(c, "firstlast+hack@googlemail.com")
    conn = sqlite3.connect(str(tmp_path / "dogfood.db"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE email_norm IS NULL").fetchone()[0] == 0
        plan = " ".join(r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM users WHERE email_norm = 'x' AND id != 'y'"))
        assert "idx_users_email_norm" in plan
    finally:
        conn.close()


def test_abuse_panel_shows_votes_of_flagged_accounts(tmp_path):
    with portal(tmp_path) as c:
        _window(c, OPEN)
        _new_user(c, "ab@gmail.com")
        twin = _new_user(c, "a.b+2@gmail.com")                             # flagged duplicate_email
        assert _vote(c, twin, "prj_02").status_code == 201
        page = c.get("/organizer", headers=ORGANIZER).text
        row = page.split("a.b+2@gmail.com", 1)[1].split("</tr>", 1)[0]
        assert "1 vote in this event" in row
