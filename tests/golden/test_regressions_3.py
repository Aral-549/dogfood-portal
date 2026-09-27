"""Regression cases for BUGLOG.md BUG-16 .. BUG-22 (third verification pass, 2026-09-27).

Written from the BUGLOG symptom text and the contracts, not from the code's output.
No mocks or monkeypatching: every HTTP test boots the real app through conftest.portal()
(or the same TestClient path with a different fixture file). Where a bug needs an external
condition (an old volume, a concurrent writer holding the database lock), the test creates it
for real with the sqlite3 stdlib module against the portal's own data directory.
A test that fails here means the implementation is wrong: keep it failing, fix the code.
"""

import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from conftest import (FIXTURES_PATH, JUDGE_A, JUDGE_B, ORGANIZER, PARTICIPANT, env,
                      load_fixture_json, portal)
from dogfood import db
from dogfood.core import importer
from dogfood.core.timeutil import format_utc

JSON_CT = {"Content-Type": "application/json"}
JSON = {"Accept": "application/json"}
NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
FUTURE_CLOSE = "2099-01-01T00:00:00Z"
DEMO_LOGINS = ["organizer@dogfood.local", "diego.herrera@example.org", "jonas.vogel@example.org",
               "priya1@example.org"]
DEMO_TOKENS = ["demo-organizer", "demo-judge-a", "demo-judge-b", "demo-participant"]


def fresh_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def raw_json(c, path, body: bytes, headers=None):
    return c.post(path, content=body, headers={**JSON_CT, **(headers or {})})


def audit(c, action):
    return [json.loads(r[0]) for r in c.app.state.conn.execute(
        "SELECT detail FROM audit_log WHERE action = ? ORDER BY id", (action,))]


# --- BUG-16: lone surrogates, deep nesting, infinite k -------------------------------------------

SURROGATE_CASES = [
    ("/login", b'{"email":"a@b.c","password":"\\ud800abc"}', None),
    ("/register", b'{"email":"surrogate@b.c","password":"\\ud800\\ud800\\ud800\\ud800xyz"}', None),
    ("/register", b'{"email":"surr\\udfffogate2@b.c","password":"long-enough-1"}', None),
    ("/set-password/abc", b'{"password":"\\ud800\\ud800\\ud800\\ud800x"}', None),
    ("/organizer/event", b'{"name":"\\udfff"}', ORGANIZER),
    ("/organizer/events", b'{"name":"\\ud800","submissions_close":"2099-01-01T00:00:00Z"}', ORGANIZER),
    ("/organizer/judges", b'{"email":"\\ud800@x.y"}', ORGANIZER),
    ("/organizer/judges/jdg_04/exclude", b'{"reason":"\\ud800"}', ORGANIZER),
    ("/organizer/judges/jdg_04/exclude", b'{"\\ud800":"key","reason":"ok"}', ORGANIZER),
    ("/api/judge/scores/prj_06", b'{"functionality":3,"quality":3,"innovation":3,"comment":"\\ud800"}', JUDGE_A),
    ("/api/projects", b'{"title":"\\ud800"}', PARTICIPANT),
]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with portal(tmp_path_factory.mktemp("regressions3")) as c:
        yield c


@pytest.mark.parametrize("path,body,headers", SURROGATE_CASES, ids=[f"{p} {b[:40]!r}" for p, b, _ in SURROGATE_CASES])
def test_bug16_lone_surrogate_is_never_a_500(client, path, body, headers):
    r = raw_json(client, path, body, headers)
    assert r.status_code < 500, r.text


def test_bug16_lone_surrogate_writes_nothing(client):
    n_users = client.app.state.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    r = raw_json(client, "/register", b'{"email":"nobody.surrogate@b.c","password":"pw\\ud800pw\\ud800pw"}')
    assert r.status_code == 422
    assert client.app.state.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == n_users
    r = raw_json(client, "/organizer/judges/jdg_09/exclude", b'{"reason":"\\ud800"}', ORGANIZER)
    assert r.status_code == 422
    assert client.app.state.conn.execute(
        "SELECT COUNT(*) FROM judge_exclusions WHERE judge_id = 'jdg_09'").fetchone()[0] == 0


@pytest.mark.parametrize("path,headers", [("/login", None), ("/register", None), ("/api/projects", PARTICIPANT),
                                          ("/organizer/event", ORGANIZER), ("/organizer/assign", ORGANIZER),
                                          ("/organizer/tiebreak", ORGANIZER),
                                          ("/organizer/judges/jdg_04/exclude", ORGANIZER),
                                          ("/api/judge/scores/prj_06", JUDGE_A)])
@pytest.mark.parametrize("shape", ["list", "object"])
def test_bug16_deep_nesting_is_never_a_500(client, path, headers, shape):
    body = b"[" * 100_000 if shape == "list" else b'{"a":' * 100_000
    r = raw_json(client, path, body, headers)
    assert r.status_code < 500


@pytest.mark.parametrize("path", ["/organizer/assign", "/organizer/tiebreak"])
@pytest.mark.parametrize("k", [b"1e999", b"-1e999", b"NaN", b"Infinity", b"1e308"])
def test_bug16_non_finite_k_is_never_a_500(client, path, k):
    r = raw_json(client, path, b'{"k": ' + k + b"}", ORGANIZER)
    assert r.status_code < 500


# --- BUG-17: a failed request must not leave a transaction open --------------------------------

def _db_file(tmp_path):
    return str(tmp_path / "dogfood.db")


def _writable(path) -> bool:
    """A fresh connection can take the write lock at once: nobody left a transaction open."""
    other = sqlite3.connect(path, timeout=1.0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


def test_bug17_request_failing_on_a_locked_database_does_not_poison_later_writes(tmp_path):
    with portal(tmp_path) as c:
        n_events = c.app.state.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        # A real concurrent writer holds the write lock longer than the portal's busy timeout,
        # so the event-create request fails inside the handler.
        blocker = sqlite3.connect(_db_file(tmp_path), isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            c.post("/organizer/events", json={"name": "Blocked", "submissions_close": FUTURE_CLOSE},
                   headers=ORGANIZER)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        assert _writable(_db_file(tmp_path))
        assert not c.app.state.conn.in_transaction
        # Every later write works: event create, score, exclusion.
        r = c.post("/organizer/events", json={"name": "After", "submissions_close": FUTURE_CLOSE}, headers=ORGANIZER)
        assert r.status_code == 303
        names = [r[0] for r in c.app.state.conn.execute("SELECT name FROM events")]
        assert "After" in names and "Blocked" not in names
        assert len(names) == n_events + 1
        assert c.post("/api/judge/scores/prj_06", json={"functionality": 3, "quality": 3, "innovation": 3},
                      headers=JUDGE_A).status_code == 200
        assert c.post("/organizer/judges/jdg_04/exclude", json={"reason": "r"}, headers=ORGANIZER).status_code == 303


def test_bug17_concurrent_writes_no_500_no_lost_writes_no_open_transaction(tmp_path):
    fx = load_fixture_json()
    a_projects = sorted({s["project"] for s in fx["scores"] if s["judge"] == "jdg_24"})
    b_projects = sorted({s["project"] for s in fx["scores"] if s["judge"] == "jdg_26"})
    assert len(a_projects) == 11 and len(b_projects) == 10
    statuses: list[tuple[str, int]] = []
    lock = threading.Lock()

    def record(tag, r):
        with lock:
            statuses.append((tag, r.status_code))

    def final_scores(i):
        return {"functionality": 5, "quality": 1 + i % 5, "innovation": 2}

    with portal(tmp_path) as c:
        def score(headers, i, project):
            for v in (1, 2, 3, 4):
                record("score", c.post(f"/api/judge/scores/{project}",
                                       json={"functionality": v, "quality": v, "innovation": v}, headers=headers))
            record("score", c.post(f"/api/judge/scores/{project}", json=final_scores(i), headers=headers))

        def exclusions():
            for n in range(6):
                record("exclude", c.post("/organizer/judges/jdg_04/exclude", json={"reason": f"r{n}"},
                                         headers=ORGANIZER))
                record("include", c.post("/organizer/judges/jdg_04/include", headers=ORGANIZER))

        def event_updates():
            for n in range(6):
                record("event", c.post("/organizer/event", json={"prizes": f"p{n}"}, headers=ORGANIZER))
            record("event", c.post("/organizer/event", json={"prizes": "final"}, headers=ORGANIZER))

        def readers():
            for _ in range(3):
                for path in ("/organizer", "/api/confidence.csv", "/api/export.csv", "/api/judge-agreement"):
                    record("read", c.get(path, headers=ORGANIZER))

        def misc_writes(n):
            record("tiebreak", c.post("/organizer/tiebreak", json={"k": 3}, headers=ORGANIZER))
            record("register", c.post("/register", data={"email": f"conc{n}@example.org",
                                                          "password": "concurrent-pass"}))
            record("rubric", c.post("/organizer/rubric", json={"w_functionality": 1}, headers=ORGANIZER))

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(score, JUDGE_A, i, p) for i, p in enumerate(a_projects)]
            futures += [pool.submit(score, JUDGE_B, i, p) for i, p in enumerate(b_projects)]
            futures += [pool.submit(exclusions), pool.submit(event_updates), pool.submit(readers)]
            futures += [pool.submit(misc_writes, n) for n in range(4)]
            for f in futures:
                f.result()

        assert [s for s in statuses if s[1] >= 500] == []
        assert all(code == 200 for tag, code in statuses if tag in ("score", "read")), statuses
        assert all(code == 303 for tag, code in statuses if tag in ("exclude", "include", "event", "tiebreak",
                                                                    "register", "rubric")), statuses
        # No lost writes: each project holds its thread's LAST write.
        mine_a = {s["project"]: s["criteria"] for s in c.get("/api/judge/scores", headers=JUDGE_A).json()["scores"]}
        mine_b = {s["project"]: s["criteria"] for s in c.get("/api/judge/scores", headers=JUDGE_B).json()["scores"]}
        for i, p in enumerate(a_projects):
            assert mine_a[p] == final_scores(i), p
        for i, p in enumerate(b_projects):
            assert mine_b[p] == final_scores(i), p
        conn = c.app.state.conn
        assert conn.execute("SELECT prizes FROM events WHERE id = 'evt_01'").fetchone()[0] == "final"
        assert conn.execute("SELECT COUNT(*) FROM judge_exclusions").fetchone()[0] == 0   # ended with include
        # Audited writes: one audit row per successful write.
        assert len(audit(c, "judge.exclude")) == 6
        assert len(audit(c, "judge.include")) == 6
        assert len(audit(c, "score.submit")) == 5 * (11 + 10)
        assert len(audit(c, "event.update")) == 7
        assert not conn.in_transaction
        assert _writable(_db_file(tmp_path))
        assert c.post("/organizer/event", json={"prizes": "after"}, headers=ORGANIZER).status_code == 303


# --- BUG-18: audited writes are one transaction ----------------------------------------------------

def _event(c):
    return dict(c.app.state.conn.execute("SELECT * FROM events WHERE id = 'evt_01'").fetchone())


def test_bug18_failed_event_update_changes_nothing_and_audits_nothing(tmp_path):
    with portal(tmp_path) as c:
        before = _event(c)
        # Valid new close, invalid judging close: nothing may be saved.
        r = c.post("/organizer/event", json={"submissions_close": FUTURE_CLOSE, "judging_close": "not a date"},
                   headers=ORGANIZER)
        assert r.status_code == 422
        # Valid new close, name that cannot be stored (lone surrogate).
        r = raw_json(c, "/organizer/event",
                     b'{"submissions_close":"2099-01-01T00:00:00Z","name":"\\ud800"}', ORGANIZER)
        assert r.status_code == 422
        assert _event(c) == before
        assert audit(c, "event.update") == []


def test_bug18_successful_event_update_has_exactly_one_audit_row_with_old_and_new(tmp_path):
    # lifecycle.md case 12: "saved, audit row with old and new value".
    with portal(tmp_path) as c:
        old = _event(c)["submissions_close"]
        assert c.post("/organizer/event", json={"submissions_close": FUTURE_CLOSE}, headers=ORGANIZER).status_code == 303
        rows = audit(c, "event.update")
        assert len(rows) == 1
        assert rows[0]["before"]["submissions_close"] == old == "2026-03-01T18:00:00Z"
        assert rows[0]["after"]["submissions_close"] == FUTURE_CLOSE
        assert _event(c)["submissions_close"] == FUTURE_CLOSE


def test_bug18_failed_rubric_update_changes_nothing(tmp_path):
    with portal(tmp_path) as c:
        crit = lambda: dict(c.app.state.conn.execute("SELECT name, weight FROM criteria WHERE event_id = 'evt_01'"))
        before = crit()
        r = c.post("/organizer/rubric", json={"w_functionality": 2, "w_quality": -1}, headers=ORGANIZER)
        assert r.status_code == 422
        assert crit() == before
        assert audit(c, "rubric.update") == []


def test_bug18_exclusion_and_its_audit_row_land_together(tmp_path):
    with portal(tmp_path) as c:
        assert c.post("/organizer/judges/jdg_04/exclude", json={"reason": "why"}, headers=ORGANIZER).status_code == 303
        assert c.app.state.conn.execute("SELECT reason FROM judge_exclusions WHERE judge_id = 'jdg_04'").fetchone()[0] == "why"
        assert audit(c, "judge.exclude") == [{"reason": "why"}]


# --- BUG-19: demo teardown on a volume created before demo_accounts existed --------------------------

def test_bug19_old_volume_demo_passwords_and_tokens_dead_when_demo_off(tmp_path):
    with portal(tmp_path, demo=True) as c:
        for email in DEMO_LOGINS:  # control: demo mode really gave these a known password
            assert c.post("/login", data={"email": email, "password": "dogfood-demo"}).status_code == 303, email
            c.cookies.clear()
    # Simulate the first release's volume: it had no demo_accounts rows.
    old = sqlite3.connect(_db_file(tmp_path), isolation_level=None)
    old.execute("DELETE FROM demo_accounts")
    old.close()
    with portal(tmp_path, demo=False) as c:
        for email in DEMO_LOGINS:
            r = c.post("/login", data={"email": email, "password": "dogfood-demo"})
            assert r.status_code == 401, email
            c.cookies.clear()
        for token in DEMO_TOKENS:
            r = c.get("/api/judge/scores", headers={"Authorization": f"Bearer {token}", **JSON})
            assert r.status_code == 401, token
        assert c.post("/organizer/events", json={"submissions_close": FUTURE_CLOSE},
                      headers={"Authorization": "Bearer demo-organizer", **JSON}).status_code == 401


# --- BUG-20: importer rubric chosen only by rows that can be imported --------------------------------

def test_bug20_rejected_rows_cannot_outvote_the_real_rubric():
    data = copy.deepcopy(load_fixture_json())
    data["scores"] += [{"judge": f"jdg_ghost_{i}", "project": "prj_01", "criteria": {"x": 3}, "comment": ""}
                       for i in range(127)]
    conn = fresh_conn()
    report = importer.import_fixtures(conn, data, NOW, "secret")
    names = {r[0] for r in conn.execute("SELECT name FROM criteria WHERE event_id = 'evt_01'")}
    assert names == {"functionality", "quality", "innovation"}
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 126
    assert len(report.rejected) == 127


def test_bug20_all_empty_criteria_imports_no_empty_reviews():
    data = copy.deepcopy(load_fixture_json())
    for s in data["scores"]:
        s["criteria"] = {}
    conn = fresh_conn()
    report = importer.import_fixtures(conn, data, NOW, "secret")
    # No rubric can be derived, so every score row is rejected; no review without scores exists.
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reviews WHERE id NOT IN "
                        "(SELECT review_id FROM review_scores)").fetchone()[0] == 0
    assert len([r for r in report.rejected if r["entity"] == "score"]) == 126


def test_bug20_rubric_tie_is_reported():
    data = copy.deepcopy(load_fixture_json())
    for s in data["scores"][:63]:
        s["criteria"] = {"alpha": 3, "beta": 3}
    conn = fresh_conn()
    report = importer.import_fixtures(conn, data, NOW, "secret")
    assert any("rubric" in n for n in report.notes), report.notes


# --- BUG-21: years before 1000 are zero-padded ---------------------------------------------------

@pytest.mark.parametrize("dt,text", [
    (datetime(999, 1, 1, tzinfo=timezone.utc), "0999-01-01T00:00:00Z"),
    (datetime(1, 1, 1, tzinfo=timezone.utc), "0001-01-01T00:00:00Z"),
    (datetime(45, 6, 7, 8, 9, 10, tzinfo=timezone.utc), "0045-06-07T08:09:10Z"),
    (datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc), "9999-12-31T23:59:59Z"),
])
def test_bug21_format_utc_zero_pads_year(dt, text):
    assert format_utc(dt) == text


def test_bug21_close_before_year_1000_stored_padded_and_pages_work(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/organizer/event", json={"submissions_close": "0999-01-01T00:00:00Z"}, headers=ORGANIZER)
        assert r.status_code == 303
        assert _event(c)["submissions_close"] == "0999-01-01T00:00:00Z"
        assert c.get("/me", headers=PARTICIPANT).status_code == 200
        assert c.get("/organizer", headers=ORGANIZER).status_code == 200
        r = c.get("/projects/new", headers={**PARTICIPANT, **JSON})
        assert r.status_code == 403 and r.json() == {"error": "submissions_closed"}
        r = c.post("/api/projects", json={"title": "Late"}, headers=PARTICIPANT)
        assert r.status_code == 403
        r = c.post("/organizer/events", json={"name": "Ancient", "submissions_close": "0999-01-01T00:00:00Z"},
                   headers=ORGANIZER)
        assert r.status_code == 303
        stored = c.app.state.conn.execute("SELECT submissions_close FROM events WHERE name = 'Ancient'").fetchone()[0]
        assert stored == "0999-01-01T00:00:00Z"


# --- BUG-22: boot and scoring gaps -------------------------------------------------------------------

def test_bug22a_registered_demo_organizer_email_is_not_hijacked(tmp_path):
    with portal(tmp_path, demo=False) as c:
        r = c.post("/register", data={"email": "organizer@dogfood.local", "password": "my-own-password-1"})
        assert r.status_code == 303
    with portal(tmp_path, demo=True) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/api/judge/scores", headers=JUDGE_A).status_code == 200   # rest of demo mode works
        assert c.post("/login", data={"email": "organizer@dogfood.local",
                                      "password": "dogfood-demo"}).status_code == 401
        r = c.post("/login", data={"email": "organizer@dogfood.local", "password": "my-own-password-1"})
        assert r.status_code == 303
        cookie = {"Cookie": f"session={r.cookies.get('session')}", **JSON}
        c.cookies.clear()
        assert c.get("/organizer", headers=cookie).status_code == 403
        assert c.post("/organizer/events", json={"submissions_close": FUTURE_CLOSE}, headers=cookie).status_code == 403
        assert c.get("/organizer", headers={**ORGANIZER, **JSON}).status_code == 401


def test_bug22b_review_given_before_joining_the_team_stops_counting(tmp_path):
    # fixtures.json: prj_06 (team tm_06) was reviewed by jdg_24, jdg_26, jdg_09 -> 3 reviews.
    fx = load_fixture_json()
    assert sorted(s["judge"] for s in fx["scores"] if s["project"] == "prj_06") == ["jdg_09", "jdg_24", "jdg_26"]
    with portal(tmp_path) as c:
        def n_reviews():
            import csv
            import io
            rows = csv.DictReader(io.StringIO(c.get("/api/export.csv", headers=ORGANIZER).text))
            return next(int(r["n_reviews"]) for r in rows if r["project_id"] == "prj_06")
        assert n_reviews() == 3
        assert c.post("/organizer/event", json={"submissions_close": FUTURE_CLOSE}, headers=ORGANIZER).status_code == 303
        code = c.app.state.conn.execute("SELECT invite_code FROM teams WHERE id = 'tm_06'").fetchone()[0]
        assert c.post(f"/join/{code}", headers=JUDGE_A).status_code == 303
        assert n_reviews() == 2


@pytest.mark.parametrize("name", [123, ["x"], {"a": 1}, True])
def test_bug22c_non_string_event_name_is_a_fixture_error(name):
    data = copy.deepcopy(load_fixture_json())
    data["event"]["name"] = name
    with pytest.raises(importer.FixtureError):
        importer.import_fixtures(fresh_conn(), data, NOW, "secret")


@pytest.mark.parametrize("event_id", ["   ", "", "\t"])
def test_bug22c_blank_event_id_is_a_fixture_error(event_id):
    data = copy.deepcopy(load_fixture_json())
    data["event"]["id"] = event_id
    with pytest.raises(importer.FixtureError):
        importer.import_fixtures(fresh_conn(), data, NOW, "secret")


@pytest.mark.parametrize("patch", [{"name": 123}, {"id": "   "}])
def test_bug22c_boot_fails_loudly_with_fixture_error(tmp_path, patch):
    from fastapi.testclient import TestClient
    from dogfood.app import app

    data = load_fixture_json()
    data["event"].update(patch)
    bad = tmp_path / "bad_event.json"
    bad.write_text(json.dumps(data))
    with env(DOGFOOD_DATA=str(tmp_path / "data"), DOGFOOD_FIXTURES=str(bad), DOGFOOD_DEMO_SESSIONS=None):
        with pytest.raises(importer.FixtureError):
            with TestClient(app, raise_server_exceptions=False):
                pass
