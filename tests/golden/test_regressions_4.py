"""Regression cases for BUGLOG.md BUG-23, BUG-26, BUG-27 (fourth verification pass, 2026-09-27).

Written from the BUGLOG symptom text and the contracts (lifecycle.md case 13,
judge-agreement.md Outputs), not from the code's output. No mocks or monkeypatching:
HTTP tests boot the real app through conftest.portal(); the BUG-27 tests start a real
uvicorn process on a free local port. Where a case needs a state that the current API
cannot create (a link left over from before the BUG-23 fix), the test writes it with the
sqlite3 stdlib module into the portal's own database, as test_regressions_3.py does.
A test that fails here means the implementation is wrong: keep it failing, fix the code.
"""

import hashlib
import http.client
import json
import math
import os
import random
import secrets
import socket
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FIXTURES_PATH, ORGANIZER, REPO, SRC, portal
from dogfood.core.agreement import judge_agreement
from dogfood.core.scoring import ScoredReview

FUTURE_CLOSE = "2099-01-01T00:00:00Z"
PARTICIPANT_EMAIL = "member1_1@example.org"   # fixtures.json: tm_01 member, no password
TEAM_PROJECT = "prj_01"                       # fixtures.json: tm_01's project
DEMO_PASSWORD = "dogfood-demo"                # contracts/acceptance.md demo logins


# --- BUG-23: an organizer must never be able to claim a participant's account ---------------------

def _uid(conn, email):
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    return row[0] if row else None


def _link_of(resp):
    loc = resp.headers.get("location", "")
    return loc.split("link=", 1)[1] if "link=" in loc else None


def _members_with_password(conn):
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT u.id FROM users u JOIN team_members m ON m.user_id = u.id "
        "WHERE u.password_hash IS NOT NULL")}


def _session_cookie(c, email, password):
    r = c.post("/login", data={"email": email, "password": password})
    c.cookies.clear()
    return {"Cookie": f"session={r.cookies.get('session')}"} if r.status_code == 303 else None


def _edit_body():
    return {"title": "hijacked", "summary": "written by the organizer", "repo_url": "https://example.org/x",
            "track_id": "trk_01", "status": "submitted"}


# Email spellings that all name the same fixture participant after the documented normalization
# (strip + lower). "K" is the Kelvin sign, whose lower() is ASCII "k".
PARTICIPANT_SPELLINGS = [
    PARTICIPANT_EMAIL,
    PARTICIPANT_EMAIL,                         # re-invite of the same address
    "MEMBER1_1@Example.ORG",
    "  member1_1@example.org  ",
    "\tmember1_1@example.org\n",
    " member1_1@example.org ",       # no-break spaces, stripped by str.strip()
]


@pytest.mark.parametrize("encoding", ["json", "form"])
def test_bug23_inviting_a_participant_never_yields_a_link(tmp_path, encoding):
    # lifecycle.md case 13: a set-password link ONLY if the account has no password yet AND is not
    # a member of any team. Every spelling of a participant's address resolves to that account.
    with portal(tmp_path) as c:
        conn = c.app.state.conn
        uid = _uid(conn, PARTICIPANT_EMAIL)
        assert uid is not None
        assert conn.execute("SELECT 1 FROM team_members WHERE user_id = ?", (uid,)).fetchone()
        before = _members_with_password(conn)
        for email in PARTICIPANT_SPELLINGS:
            body = {"email": email, "tracks": "trk_01"}
            r = (c.post("/organizer/judges", json=body, headers=ORGANIZER) if encoding == "json"
                 else c.post("/organizer/judges", data=body, headers=ORGANIZER))
            assert r.status_code == 303, (email, r.status_code)
            assert _link_of(r) is None, (email, r.headers.get("location"))
        # No stray account was created for any spelling, and nothing claimable was stored.
        assert conn.execute("SELECT COUNT(*) FROM users WHERE lower(trim(email)) LIKE '%member1_1@%'"
                            ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM password_links WHERE user_id = ?", (uid,)).fetchone()[0] == 0
        issued = [json.loads(r[0]).get("link_issued") for r in conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'judge.invite'")]
        assert issued and not any(issued)
        assert _members_with_password(conn) == before


def test_bug23_organizer_cannot_log_in_or_submit_as_the_participant(tmp_path):
    with portal(tmp_path) as c:
        conn = c.app.state.conn
        assert c.post("/organizer/event", json={"submissions_close": FUTURE_CLOSE},
                      headers=ORGANIZER).status_code == 303
        links = []
        for email in PARTICIPANT_SPELLINGS:
            links.append(_link_of(c.post("/organizer/judges", json={"email": email}, headers=ORGANIZER)))
        chosen = "organizer-chosen-pw"
        for link in filter(None, links):
            c.post(link, data={"password": chosen})
        # Guessing a link is no better than having none.
        for guess in ("/set-password/", "/set-password/x", "/set-password/" + "A" * 32):
            assert c.post(guess, data={"password": chosen}).status_code in (404, 405)
        for pw in (chosen, "", DEMO_PASSWORD, "password"):
            assert c.post("/login", data={"email": PARTICIPANT_EMAIL, "password": pw}).status_code == 401
        assert conn.execute("SELECT password_hash FROM users WHERE email = ?",
                            (PARTICIPANT_EMAIL,)).fetchone()[0] is None
        # The team's project is untouched and cannot be edited with the organizer's own credentials.
        r = c.put(f"/api/projects/{TEAM_PROJECT}", json=_edit_body(), headers=ORGANIZER)
        assert r.status_code == 403
        assert conn.execute("SELECT title FROM projects WHERE id = ?", (TEAM_PROJECT,)).fetchone()[0] != "hijacked"


def test_bug23_control_new_email_still_gets_a_working_link(tmp_path):
    # lifecycle.md case 13 control: a judge who is not a participant still gets a one-time link.
    with portal(tmp_path) as c:
        conn = c.app.state.conn
        r = c.post("/organizer/judges", json={"email": "Fresh.Judge@Example.org "}, headers=ORGANIZER)
        link = _link_of(r)
        assert link and link.startswith("/set-password/")
        assert c.post(link, data={"password": "fresh-judge-pw"}).status_code == 303
        assert c.post(link, data={"password": "second-use-pw"}).status_code == 404   # one-time
        cookie = _session_cookie(c, "fresh.judge@example.org", "fresh-judge-pw")
        assert cookie is not None
        # Holding a judge account gives no access to a team's submission.
        assert c.post("/organizer/event", json={"submissions_close": FUTURE_CLOSE},
                      headers=ORGANIZER).status_code == 303
        r = c.put(f"/api/projects/{TEAM_PROJECT}", json=_edit_body(), headers=cookie)
        assert r.status_code == 403
        assert conn.execute("SELECT title FROM projects WHERE id = ?", (TEAM_PROJECT,)).fetchone()[0] != "hijacked"


def test_bug23_lookalike_address_gets_a_separate_account_not_the_participants(tmp_path):
    # A zero-width space is not whitespace, so this is a different address. Whatever the portal
    # does with it, the account the organizer ends up holding must not be the participant's.
    lookalike = PARTICIPANT_EMAIL + "​"
    with portal(tmp_path) as c:
        conn = c.app.state.conn
        participant = _uid(conn, PARTICIPANT_EMAIL)
        link = _link_of(c.post("/organizer/judges", json={"email": lookalike}, headers=ORGANIZER))
        if link:
            assert c.post(link, data={"password": "lookalike-pw"}).status_code == 303
            cookie = _session_cookie(c, lookalike, "lookalike-pw")
            assert cookie is not None
            assert c.post("/organizer/event", json={"submissions_close": FUTURE_CLOSE},
                          headers=ORGANIZER).status_code == 303
            assert c.put(f"/api/projects/{TEAM_PROJECT}", json=_edit_body(), headers=cookie).status_code == 403
        assert conn.execute("SELECT password_hash FROM users WHERE id = ?", (participant,)).fetchone()[0] is None
        assert c.post("/login", data={"email": PARTICIPANT_EMAIL, "password": "lookalike-pw"}).status_code == 401


def test_bug23_demo_mode_off_participant_gets_no_link(tmp_path):
    # With demo mode off, boot clears the demo participant's known password (acceptance.md), so
    # priya1@example.org (tm_01) is password-less again: still a participant, still no link.
    with portal(tmp_path, demo=True):
        pass
    with portal(tmp_path, demo=False) as c:
        conn = c.app.state.conn
        assert conn.execute("SELECT password_hash FROM users WHERE email = 'priya1@example.org'"
                            ).fetchone()[0] is None
        # With demo mode off there is no organizer credential to drive the HTTP invite, so call the
        # function the invite handler calls (services.create_password_link) on the real database.
        from dogfood import services as svc
        uid = _uid(conn, "priya1@example.org")
        assert svc.create_password_link(conn, uid) is None


def test_bug23_link_issued_before_the_fix_cannot_claim_a_participant(tmp_path):
    # Adversarial: a volume from before the fix can still hold an unexpired link that the organizer
    # obtained for a participant (the BUG-23 symptom). "The organizer must never be able to claim a
    # participant's account" (lifecycle.md case 13) must hold for such a link too.
    # The stored form is the sha256 hex of the token (schema.sql: password_links.token_hash);
    # the control below proves a row written this way is a valid link for a non-participant.
    expires = (datetime.now(timezone.utc) + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with portal(tmp_path) as c:
        conn = c.app.state.conn

        def plant(uid):
            token = secrets.token_urlsafe(24)
            conn.execute("INSERT INTO password_links VALUES (?, ?, ?)",
                         (hashlib.sha256(token.encode()).hexdigest(), uid, expires))
            return "/set-password/" + token

        # Control: a fixture judge who is not a participant (jdg_01, tomas.varga@example.org).
        judge_uid = _uid(conn, "tomas.varga@example.org")
        assert not conn.execute("SELECT 1 FROM team_members WHERE user_id = ?", (judge_uid,)).fetchone()
        assert c.post(plant(judge_uid), data={"password": "planted-judge-pw"}).status_code == 303
        assert c.post("/login", data={"email": "tomas.varga@example.org",
                                      "password": "planted-judge-pw"}).status_code == 303
        # The case: the same kind of row for a participant.
        participant = _uid(conn, PARTICIPANT_EMAIL)
        c.post(plant(participant), data={"password": "legacy-link-pw"})
        assert c.post("/login", data={"email": PARTICIPANT_EMAIL, "password": "legacy-link-pw"}).status_code == 401
        assert conn.execute("SELECT password_hash FROM users WHERE id = ?", (participant,)).fetchone()[0] is None


# --- BUG-26: outlier status agrees with the reported 3 dp agreement ------------------------------

def _pearson(xs, ys):
    """Independent Pearson r (not dogfood's)."""
    n = len(xs)
    mx, my = math.fsum(xs) / n, math.fsum(ys) / n
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy)


def _one_judge_against_fixed_consensus(xs, cs):
    """Judge J reviews P1..Pn with z = xs; each Pi has exactly one other review, by Ki, with z = cs[i].
    Leave-one-out consensus for J on Pi is then exactly cs[i] (judge-agreement.md Outputs)."""
    scored = []
    for i, (x, c) in enumerate(zip(xs, cs)):
        scored.append(ScoredReview("J", f"P{i}", float(x), float(x)))
        scored.append(ScoredReview(f"K{i}", f"P{i}", float(c), float(c)))
    informative = {"J"} | {f"K{i}" for i in range(len(xs))}
    report, _ = judge_agreement(scored, informative)
    return next(a for a in report if a.judge == "J")


def _expected_status(r):
    # judge-agreement.md: thresholds compare the reported (3 dp) agreement; outlier if < -0.3.
    return "outlier" if round(r, 3) < -0.3 else "ok"


# Vectors found by exhaustive search over integer consensus values in [-6, 6] with xs = 0..4.
BOUNDARY_CASES = [
    # (consensus, window the true r must lie in, reported, status)
    ((-5, 4, 5, -4, -6), (-0.3005, -0.3), -0.3, "ok"),        # r = -0.30042: below -0.3, shown -0.300
    ((-6, 4, -5, -4, -6), (-0.3, -0.2995), -0.3, "ok"),       # r = -0.29981: above -0.3, shown -0.300
    ((-6, 4, -3, -6, -5), (-0.3015, -0.3005), -0.301, "outlier"),  # r = -0.30066: shown -0.301
]


@pytest.mark.parametrize("cs,window,reported,status", BOUNDARY_CASES)
def test_bug26_status_agrees_with_reported_agreement_at_boundary(cs, window, reported, status):
    xs = [0, 1, 2, 3, 4]
    r = _pearson(xs, cs)
    assert window[0] < r < window[1], r        # the case really sits where it is meant to
    assert _expected_status(r) == status
    row = _one_judge_against_fixed_consensus(xs, cs)
    assert row.shared == 5
    assert row.agreement == reported
    assert row.status == status


def test_bug26_status_never_disagrees_with_the_value_shown_random():
    # Property over many near-boundary inputs: for every judge with a reported agreement,
    # status == outlier exactly when the reported value is below -0.3, and the reported value
    # is r to 3 dp (within half a unit of the last place of the independent r).
    rng = random.Random(20260927)
    xs = [0, 1, 2, 3, 4, 5]
    hits = 0
    for _ in range(20000):
        cs = [rng.randint(-9, 9) for _ in xs]
        if len(set(cs)) == 1:
            continue
        r = _pearson(xs, cs)
        if not -0.31 < r < -0.29:
            continue
        hits += 1
        row = _one_judge_against_fixed_consensus(xs, cs)
        assert abs(row.agreement - r) <= 0.0005 + 1e-12, (cs, r, row)
        assert row.status == ("outlier" if row.agreement < -0.3 else "ok"), (cs, r, row)
    assert hits > 50


def test_bug26_dashboard_states_the_amended_outlier_rule(tmp_path):
    # judge-agreement.md: outlier is agreement < -0.3 over 4+ shared projects (amended 2026-09-27).
    # The organizer dashboard must not explain the status with the superseded rule
    # ("below 0 over 3+"), or a row showing -0.150 / ok contradicts the text beside it.
    with portal(tmp_path) as c:
        page = c.get("/organizer", headers=ORGANIZER).text
        assert "below 0 over 3+" not in page
        assert "-0.3" in page


# --- BUG-27: password hashing must not stall the server -----------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body)
        h["Content-Type"] = "application/json"
    t = time.perf_counter()
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    payload = resp.read()
    elapsed = time.perf_counter() - t
    loc = resp.getheader("location")
    conn.close()
    return resp.status, elapsed, loc, payload


@pytest.fixture
def live(tmp_path):
    port = _free_port()
    envv = {**os.environ, "DOGFOOD_DATA": str(tmp_path), "DOGFOOD_FIXTURES": str(FIXTURES_PATH),
            "DOGFOOD_DEMO_SESSIONS": "1", "DOGFOOD_LOG_LEVEL": "WARNING",
            "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    log = open(tmp_path / "uvicorn.log", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "dogfood.app:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=str(REPO), env=envv, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if _request(port, "GET", "/healthz")[0] == 200:
                    break
            except OSError:
                pass
            assert proc.poll() is None and time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.1)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


GOOD = {"email": "priya1@example.org", "password": DEMO_PASSWORD}
BAD = {"email": "priya1@example.org", "password": "wrong-password-1"}


def _single_login_time(port):
    return statistics.median(_request(port, "POST", "/login", BAD)[1] for _ in range(5))


def test_bug27_healthz_not_stalled_by_20_concurrent_logins(live):
    # BUG-27 symptom: concurrent logins pushed /healthz from 2 ms to 0.81 s, because scrypt ran on
    # the event loop and every request queued behind every hash.
    #
    # Bound, and why it is robust: with hashing on the loop, a /healthz issued while 20 logins are
    # in flight waits for the remaining hashes one after another, so its round trip is close to the
    # whole burst (measured on the pre-fix code: 465 ms of a 485 ms burst, ratio 0.96; 20-way
    # sustained load: median 15-16 single-login times). With hashing off the loop it is a small
    # fraction (measured on the fixed code: ratio 0.26-0.44; sustained median 1.2-1.3 single-login
    # times). The assertions sit between: best-of-3 burst ratio < 0.5, and sustained median
    # < 5 single-login times. Both are ratios, so CPU speed cancels out.
    port = live
    t1 = _single_login_time(port)
    ratios = []
    for _ in range(3):
        barrier = threading.Barrier(21)
        results = []

        def worker(i):
            barrier.wait()
            body = GOOD if i % 2 else BAD
            results.append((body is GOOD, _request(port, "POST", "/login", body)[0]))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        barrier.wait()
        start = time.perf_counter()
        time.sleep(max(0.02, t1 / 2))            # the burst is in flight
        status, hz, _, _ = _request(port, "GET", "/healthz")
        for t in threads:
            t.join()
        burst = time.perf_counter() - start
        assert status == 200
        # Correctness under concurrency: every login got its own right answer.
        assert sorted(results) == sorted([(True, 303)] * 10 + [(False, 401)] * 10)
        ratios.append(hz / burst)
    assert min(ratios) < 0.5, (ratios, t1)

    # Sustained 20-way login load for ~2 s, /healthz sampled throughout.
    stop = time.perf_counter() + 2.0

    def hammer():
        while time.perf_counter() < stop:
            _request(port, "POST", "/login", BAD)

    threads = [threading.Thread(target=hammer) for _ in range(20)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    samples = []
    while time.perf_counter() < stop - 0.3:
        samples.append(_request(port, "GET", "/healthz")[1])
        time.sleep(0.1)
    for t in threads:
        t.join()
    assert len(samples) >= 3
    assert statistics.median(samples) < 5 * t1, (samples, t1)


def test_bug27_set_password_link_is_single_use_under_concurrency(live):
    # Moving consume_password_link into the threadpool makes concurrent uses of one link run in
    # parallel. lifecycle.md case 13: the link is one-time. Exactly one use may win.
    port = live
    status, _, loc, _ = _request(port, "POST", "/organizer/judges", {"email": "race.judge@example.org"},
                                 headers={"Authorization": "Bearer demo-organizer"})
    assert status == 303 and "link=" in (loc or "")
    link = loc.split("link=", 1)[1]
    barrier = threading.Barrier(10)
    out = []

    def use(i):
        barrier.wait()
        out.append((i, _request(port, "POST", link, {"password": f"race-password-{i}"})[0]))

    threads = [threading.Thread(target=use, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [i for i, s in out if s == 303]
    assert len(winners) == 1, out
    assert all(s == 404 for i, s in out if i not in winners), out
    ok = [i for i in range(10) if _request(port, "POST", "/login", {"email": "race.judge@example.org",
                                                                    "password": f"race-password-{i}"})[0] == 303]
    assert ok == winners


def test_bug27_concurrent_register_same_email_one_account(live):
    port = live
    barrier = threading.Barrier(10)
    out = []

    def reg(i):
        barrier.wait()
        out.append(_request(port, "POST", "/register", {"email": "dup.register@example.org", "name": f"n{i}",
                                                        "password": f"dup-password-{i}"})[0])

    threads = [threading.Thread(target=reg, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(out) == [303] + [409] * 9, out
