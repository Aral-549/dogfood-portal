"""contracts/t4-api-webhooks-bulk.md and contracts/t4-records-widget.md.

HTTP cases run the real app. Webhook retry timing (api case 7) drives the delivery worker's
tick() with injected times against the portal's own sqlite file after the portal has shut
down, so no background thread races the test. Signatures are checked with the
`cryptography` library directly and, where available, with the openssl CLI (records case 4).

Fixture facts (fixtures.json, counted below with the stdlib): 30 judges have scores;
jdg_24 (demo judge A) scored 11 projects; the demo organizer is an admin in demo mode.
"""

import base64
import hashlib
import hmac
import http.server
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from conftest import (EVENT_ID, FIXTURES_PATH, JUDGE_A, ORGANIZER, PARTICIPANT, env, load_fixture_json,
                      portal)

ALL_THREES = {"functionality": 3, "quality": 3, "innovation": 3}


def _db(data_dir):
    conn = sqlite3.connect(str(data_dir / "dogfood.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _count(data_dir, sql, args=()):
    conn = _db(data_dir)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


@contextmanager
def empty_portal(data_dir):
    """DOGFOOD_FIXTURES=none: nothing seeded, no demo accounts."""
    from fastapi.testclient import TestClient
    from dogfood.app import app
    with env(DOGFOOD_DATA=str(data_dir), DOGFOOD_FIXTURES="none", DOGFOOD_DEMO_SESSIONS=None):
        with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
            yield client


class _Receiver:
    """A local webhook receiver: records (headers, raw body) and answers with `status`."""

    def __init__(self, status=200, delay=0.0, location=None):
        self.hits, self.status = [], status
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.hits.append((dict(self.headers), raw))
                time.sleep(delay)
                self.send_response(outer.status)
                if location:
                    self.send_header("Location", location)
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _wait(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# === API and tokens =========================================================================
def test_api_case1_openapi_lists_v1_routes(tmp_path):
    with portal(tmp_path) as c:
        paths = c.get("/openapi.json").json()["paths"]
        for p in ("/api/v1/projects", "/api/v1/votes", "/api/v1/tokens", "/api/v1/webhooks", "/api/v1/import",
                  "/api/v1/events/{event_id}/export.json", "/api/v1/judge/scores/{project_id}", "/api/v1/results"):
            assert p in paths, p


def test_api_case2_3_organizer_token_then_revoke(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/tokens", json={"name": "ci"}, headers=ORGANIZER)
        assert r.status_code == 201
        tok, tid = r.json()["token"], r.json()["id"]
        bearer = {"Authorization": f"Bearer {tok}"}
        assert c.get("/api/export.csv", headers=bearer).status_code == 200
        listed = c.get("/api/v1/tokens", headers=ORGANIZER).json()
        assert [t["id"] for t in listed] == [tid] and all("token" not in t for t in listed)   # shown once
        # stored hashed: the plain token appears nowhere in the database file
        assert tok.encode() not in (tmp_path / "dogfood.db").read_bytes()
        assert c.delete(f"/api/v1/tokens/{tid}", headers=ORGANIZER).status_code == 204
        assert c.get("/api/export.csv", headers=bearer).status_code == 401


def test_api_case4_judge_token_has_only_judge_roles(tmp_path):
    with portal(tmp_path) as c:
        tok = c.post("/api/v1/tokens", json={}, headers=JUDGE_A).json()["token"]
        bearer = {"Authorization": f"Bearer {tok}"}
        assert c.get("/api/v1/judge/scores", headers=bearer).status_code == 200
        assert c.get("/api/v1/judges/jdg_26/scores", headers=bearer).status_code == 403
        assert c.get("/api/v1/export.csv", headers=bearer).status_code == 403
        ptok = c.post("/api/v1/tokens", json={}, headers=PARTICIPANT).json()["token"]
        assert c.get("/api/v1/export.csv", headers={"Authorization": f"Bearer {ptok}"}).status_code == 403


@pytest.mark.parametrize("method, legacy, v1, headers", [
    ("get", "/api/export.csv", "/api/v1/export.csv", {}),
    ("get", "/api/export.csv", "/api/v1/export.csv", PARTICIPANT),
    ("get", "/api/export.csv", "/api/v1/export.csv", JUDGE_A),
    ("get", "/api/export.csv", "/api/v1/export.csv", ORGANIZER),
    ("get", "/api/judges/jdg_26/scores", "/api/v1/judges/jdg_26/scores", JUDGE_A),
    ("get", "/api/judges/jdg_26/scores", "/api/v1/judges/jdg_26/scores", ORGANIZER),
    ("get", "/api/judges/jdg_26/scores", "/api/v1/judges/jdg_26/scores", {}),
    ("get", "/api/judge/scores", "/api/v1/judge/scores", PARTICIPANT),
    ("get", "/api/confidence.csv", "/api/v1/confidence.csv", JUDGE_A),
    ("get", "/api/judge-agreement", "/api/v1/judge-agreement", PARTICIPANT),
    ("post", "/api/judge/scores/prj_06", "/api/v1/judge/scores/prj_06", PARTICIPANT),
    ("post", "/api/judge/scores/prj_06", "/api/v1/judge/scores/prj_06", {}),
    ("post", "/api/projects", "/api/v1/projects", PARTICIPANT),     # after close: submissions_closed
])
def test_api_case5_v1_twins_same_status(tmp_path, method, legacy, v1, headers):
    with portal(tmp_path) as c:
        kw = {"json": ALL_THREES} if method == "post" else {}
        a = getattr(c, method)(legacy, headers=headers, **kw)
        b = getattr(c, method)(v1, headers=headers, **kw)
        assert a.status_code == b.status_code, (legacy, a.status_code, b.status_code)


def test_api_edge_bearer_token_exempt_from_csrf(tmp_path):
    with portal(tmp_path) as c:
        tok = c.post("/api/v1/tokens", json={}, headers=ORGANIZER).json()["token"]
        r = c.post("/api/v1/tokens", json={}, headers={"Authorization": f"Bearer {tok}",
                                                        "Origin": "https://evil.example"})
        assert r.status_code == 201


# === webhooks ================================================================================
def test_api_case8_9_webhook_validation_and_authz(tmp_path):
    with portal(tmp_path) as c:
        ok = {"url": "https://example.org/hook", "secret": "s" * 16, "events": ["score.submitted"]}
        for bad in ({"url": "ftp://example.org/x"}, {"url": "example.org"}, {"secret": "s" * 15},
                    {"events": []}, {"events": ["nope"]}):
            assert c.post("/api/v1/webhooks", json={**ok, **bad}, headers=ORGANIZER).status_code == 422, bad
        for h in (PARTICIPANT, JUDGE_A):
            assert c.post("/api/v1/webhooks", json=ok, headers=h).status_code == 403
        assert c.post("/api/v1/webhooks", json=ok).status_code == 401
        r = c.post("/api/v1/webhooks", json=ok, headers=ORGANIZER)
        assert r.status_code == 201 and "secret" not in r.json()
        listed = c.get("/api/v1/webhooks", headers=ORGANIZER).json()
        assert len(listed) == 1 and "secret" not in listed[0]                    # edge: never returned


def test_api_case6_score_submitted_one_signed_delivery_without_values(tmp_path):
    rx = _Receiver()
    secret = "a-very-secret-secret"
    try:
        with portal(tmp_path) as c:
            r = c.post("/api/v1/webhooks", json={"url": rx.url, "secret": secret, "events": ["score.submitted"]},
                       headers=ORGANIZER)
            assert r.status_code == 201
            assert c.post("/api/v1/judge/scores/prj_06", json={"functionality": 4, "quality": 2, "innovation": 5},
                          headers=JUDGE_A).status_code == 200
            assert _wait(lambda: len(rx.hits) >= 1)
            time.sleep(1.5)                          # a second worker poll: still exactly one
            assert len(rx.hits) == 1
            headers, raw = rx.hits[0]
            expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            assert headers["X-Dogfood-Signature"] == expected
            body = json.loads(raw)
            assert set(body) == {"id", "event", "created_at", "data"} and body["event"] == "score.submitted"
            assert body["data"].get("project") == "prj_06"
            flat = json.dumps(body["data"])
            for k in ("functionality", "quality", "innovation", "criteria", "value"):
                assert k not in flat
            log = c.get(f"/api/v1/webhooks/{r.json()['id']}/deliveries", headers=ORGANIZER).json()
            assert log[0]["state"] == "delivered" and log[0]["attempt_log"][0]["status"] == 200
            assert "body" not in log[0]                                          # status codes, not bodies
    finally:
        rx.close()


def test_api_case7_retries_10_60_300_then_failed(tmp_path):
    from dogfood import db
    from dogfood.webhook_worker import Worker, enqueue
    rx = _Receiver(status=500)
    try:
        with portal(tmp_path) as c:
            assert c.post("/api/v1/webhooks", json={"url": rx.url, "secret": "x" * 16, "events": ["results.published"]},
                          headers=ORGANIZER).status_code == 201
        # Portal (and its worker thread) are stopped. Drive the worker by hand with injected times.
        t0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
        conn = db.connect(str(tmp_path / "dogfood.db"))
        with db.transaction(conn):
            assert enqueue(conn, EVENT_ID, "results.published", {"event": EVENT_ID}, t0) == 1
        w = Worker(str(tmp_path / "dogfood.db"))
        state = lambda: conn.execute("SELECT state, attempts FROM webhook_deliveries").fetchone()
        w.tick(conn, t0)
        assert tuple(state()) == ("pending", 1)
        w.tick(conn, t0 + timedelta(seconds=9))                                  # not yet due
        assert tuple(state()) == ("pending", 1)
        w.tick(conn, t0 + timedelta(seconds=10))
        assert tuple(state()) == ("pending", 2)
        w.tick(conn, t0 + timedelta(seconds=10 + 59))
        assert tuple(state()) == ("pending", 2)
        w.tick(conn, t0 + timedelta(seconds=10 + 60))
        assert tuple(state()) == ("pending", 3)
        w.tick(conn, t0 + timedelta(seconds=10 + 60 + 300))
        assert tuple(state()) == ("failed", 4)
        w.tick(conn, t0 + timedelta(days=1))
        assert tuple(state()) == ("failed", 4)
        attempts = conn.execute("SELECT attempt, status FROM delivery_attempts ORDER BY attempt").fetchall()
        assert [tuple(a) for a in attempts] == [(1, 500), (2, 500), (3, 500), (4, 500)]
        assert len(rx.hits) == 4
        conn.close()
    finally:
        rx.close()


def test_api_case7_unreachable_receiver_logged_and_request_not_blocked(tmp_path):
    # Port 9 on localhost (discard) is closed in CI containers: connection refused immediately.
    with portal(tmp_path) as c:
        c.post("/api/v1/webhooks", json={"url": "http://127.0.0.1:9/x", "secret": "x" * 16,
                                         "events": ["score.submitted"]}, headers=ORGANIZER)
        start = time.monotonic()
        assert c.post("/api/v1/judge/scores/prj_06", json=ALL_THREES, headers=JUDGE_A).status_code == 200
        assert time.monotonic() - start < 2.0
        wid = c.get("/api/v1/webhooks", headers=ORGANIZER).json()[0]["id"]
        assert _wait(lambda: c.get(f"/api/v1/webhooks/{wid}/deliveries", headers=ORGANIZER).json()[0]["attempts"] >= 1)
        d = c.get(f"/api/v1/webhooks/{wid}/deliveries", headers=ORGANIZER).json()[0]
        assert d["state"] == "pending" and d["attempt_log"][0]["status"] is None and d["attempt_log"][0]["error"]


def _worker_setup(tmp_path, urls):
    """Portal stopped, one webhook per url, one delivery each enqueued at T0; returns (conn, worker, T0)."""
    from dogfood import db
    from dogfood.webhook_worker import Worker, enqueue
    with portal(tmp_path) as c:
        for u in urls:
            assert c.post("/api/v1/webhooks", json={"url": u, "secret": "x" * 16, "events": ["results.published"]},
                          headers=ORGANIZER).status_code == 201
    t0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
    conn = db.connect(str(tmp_path / "dogfood.db"))
    with db.transaction(conn):
        assert enqueue(conn, EVENT_ID, "results.published", {"event": EVENT_ID}, t0) == len(urls)
    return conn, Worker(str(tmp_path / "dogfood.db")), t0


def test_worker_slow_receivers_do_not_serialize(tmp_path):
    slow = [_Receiver(delay=1.5) for _ in range(4)]
    try:
        conn, w, t0 = _worker_setup(tmp_path, [r.url for r in slow])
        start = time.monotonic()
        w.tick(conn, t0)
        took = time.monotonic() - start
        assert took < 4.0, took                       # one after another would take >= 6 s
        assert [r[0] for r in conn.execute("SELECT state FROM webhook_deliveries")] == ["delivered"] * 4
        conn.close()
    finally:
        for r in slow:
            r.close()


def test_worker_redirect_is_a_failed_attempt_not_followed(tmp_path):
    target = _Receiver()
    bouncer = _Receiver(status=302, location=target.url)
    try:
        conn, w, t0 = _worker_setup(tmp_path, [bouncer.url])
        w.tick(conn, t0)
        assert tuple(conn.execute("SELECT state, last_status FROM webhook_deliveries").fetchone()) == ("pending", 302)
        assert len(bouncer.hits) == 1 and target.hits == []
        conn.close()
    finally:
        target.close()
        bouncer.close()


def test_worker_prunes_old_finished_deliveries(tmp_path):
    rx = _Receiver()
    try:
        conn, w, t0 = _worker_setup(tmp_path, [rx.url])
        w.tick(conn, t0)
        assert conn.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 1
        from dogfood.webhook_worker import enqueue
        from dogfood import db
        with db.transaction(conn):
            enqueue(conn, EVENT_ID, "results.published", {"event": EVENT_ID}, t0 + timedelta(days=31))
        w.tick(conn, t0 + timedelta(days=31))           # delivers the new one, prunes the 31-day-old one
        assert conn.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0] == 1
        conn.close()
    finally:
        rx.close()


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data", "http://[fe80::1]/x", "http://0.0.0.0/x",
                                 "http://224.0.0.1/x"])
def test_webhook_refuses_metadata_and_odd_addresses(tmp_path, url):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/webhooks", json={"url": url, "secret": "s" * 16, "events": ["score.submitted"]},
                   headers=ORGANIZER)
        assert r.status_code == 422


def test_webhook_to_the_portal_itself_still_allowed(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/webhooks", json={"url": "http://127.0.0.1:8080/healthz", "secret": "s" * 16,
                                             "events": ["score.submitted"]}, headers=ORGANIZER)
        assert r.status_code == 201


def test_api_case10_no_webhooks_no_deliveries(tmp_path):
    with portal(tmp_path) as c:
        assert c.post("/api/v1/judge/scores/prj_06", json=ALL_THREES, headers=JUDGE_A).status_code == 200
        assert c.post("/api/v1/results/publish", json={}, headers=ORGANIZER).status_code == 303
    assert _count(tmp_path, "SELECT COUNT(*) FROM webhook_deliveries") == 0


# === export / import =========================================================================
def test_api_case14_15_export_authz_and_content(tmp_path):
    with portal(tmp_path) as c:
        url = f"/api/v1/events/{EVENT_ID}/export.json"
        assert c.get(url).status_code == 401
        assert c.get(url, headers=JUDGE_A).status_code == 403
        assert c.get(url, headers=PARTICIPANT).status_code == 403
        r = c.get(url, headers=ORGANIZER)
        assert r.status_code == 200
        data = r.json()
        for k in ("event", "tracks", "judges", "teams", "projects", "scores", "rubric", "exclusions", "comments"):
            assert k in data, k
        assert "votes" not in data or data["votes"] in (None, [])               # window not closed
        raw = r.text
        for secret in ("password_hash", "token_hash", "demo-organizer", "demo-judge-a", "scrypt"):
            assert secret not in raw, secret


def _ranks(c, headers):
    return [(r["rank"], r["project"]) for r in c.get(f"/api/v1/results?event={EVENT_ID}", headers=headers).json()]


def test_api_case11_lossless_round_trip(tmp_path, capsys):
    from dogfood import cli
    a, b = tmp_path / "a", tmp_path / "b"
    with portal(a) as c:
        exported = c.get(f"/api/v1/events/{EVENT_ID}/export.json", headers=ORGANIZER).json()
        ranks_a = _ranks(c, ORGANIZER)
    assert len(ranks_a) == 40
    b.mkdir()
    with env(DOGFOOD_DATA=str(b)):
        assert cli.create_admin("root@example.org") == 0
    link = capsys.readouterr().out.split("/set-password/", 1)[1].strip()
    with empty_portal(b) as c:
        assert c.get("/api/v1/events").json() == []
        assert c.post(f"/set-password/{link}", data={"password": "root-password"}).status_code == 303
        assert c.post("/login", data={"email": "root@example.org", "password": "root-password"}).status_code == 303
        tok = c.post("/api/v1/tokens", json={}).json()["token"]
        c.cookies.clear()
        admin = {"Authorization": f"Bearer {tok}"}
        r = c.post("/api/v1/import", json=exported, headers=admin)
        assert r.status_code == 201, r.text
        assert _ranks(c, admin) == ranks_a
        again = c.get(f"/api/v1/events/{EVENT_ID}/export.json", headers=admin).json()
        for k in ("tracks", "judges", "teams", "projects", "scores"):
            key = lambda x: json.dumps(x, sort_keys=True)
            assert sorted(map(key, again[k])) == sorted(map(key, exported[k])), k
        # case 12: importing the same event again
        before = _count(b, "SELECT COUNT(*) FROM projects")
        assert c.post("/api/v1/import", json=exported, headers=admin).status_code == 409
        assert _count(b, "SELECT COUNT(*) FROM projects") == before


def test_api_case12_13_import_conflict_malformed_and_authz(tmp_path):
    with portal(tmp_path) as c:
        fixture = load_fixture_json()
        tables = ("events", "tracks", "teams", "projects", "users", "reviews")
        before = [_count(tmp_path, f"SELECT COUNT(*) FROM {t}") for t in tables]
        r = c.post("/api/v1/import", json=fixture, headers=ORGANIZER)
        assert (r.status_code, r.json()["error"]) == (409, "event_exists")                  # case 12
        # A new event id whose projects/teams reuse evt_01's ids must not rewrite evt_01.
        hijack = {**fixture, "event": {**fixture["event"], "id": "evt_new"},
                  "projects": [{**fixture["projects"][0], "title": "HIJACKED"}] + fixture["projects"][1:]}
        r = c.post("/api/v1/import", json=hijack, headers=ORGANIZER)
        assert (r.status_code, r.json()["error"]) == (409, "ids_in_use") and "tracks:trk_01" in r.json()["ids"]
        assert "HIJACKED" not in c.get("/api/v1/projects").text
        for bad in ({"nonsense": 1}, {"event": {"id": "evt_x"}}, {**fixture, "event": "evt_x"}):   # case 13
            r = c.post("/api/v1/import", json=bad, headers=ORGANIZER)
            assert r.status_code == 422 and r.json()["detail"], bad
        after = [_count(tmp_path, f"SELECT COUNT(*) FROM {t}") for t in tables]
        assert after == before                                                   # nothing written
        assert c.post("/api/v1/import", json=fixture, headers=PARTICIPANT).status_code == 403
        assert c.post("/api/v1/import", json=fixture).status_code == 401


def _admin_in_empty_portal(b, capsys):
    """create-admin via the CLI (as an operator would), returning the set-password link path."""
    from dogfood import cli
    b.mkdir(exist_ok=True)
    with env(DOGFOOD_DATA=str(b)):
        assert cli.create_admin("root@example.org") == 0
    return "/set-password/" + capsys.readouterr().out.split("/set-password/", 1)[1].strip()


def _admin_token(c, link):
    assert c.post(link, data={"password": "root-password"}).status_code == 303
    assert c.post("/login", data={"email": "root@example.org", "password": "root-password"}).status_code == 303
    tok = c.post("/api/v1/tokens", json={}).json()["token"]
    c.cookies.clear()
    return {"Authorization": f"Bearer {tok}"}


def _key(x):
    return json.dumps(x, sort_keys=True)


def test_round_trip_mid_event_keeps_activity(tmp_path, capsys):
    """Beyond case 11: drafts, pending assignments, comments, votes, voided voters and
    co-organizers survive export -> import (a move in the middle of an event)."""
    a, b = tmp_path / "a", tmp_path / "b"
    with portal(a) as c:
        r = c.post("/api/v1/event", json={"submissions_close": "2099-01-01T00:00:00Z",
                                          "voting_open": "2026-01-01T00:00:00Z",
                                          "voting_close": "2099-01-01T00:00:00Z"}, headers=ORGANIZER)
        assert r.status_code == 303
        assert c.post("/api/v1/projects", json={"title": "Half built", "draft": True},
                      headers=PARTICIPANT).status_code == 201
        assert c.post("/api/v1/assignments/auto", json={}, headers=ORGANIZER).status_code in (200, 303)
        assert c.post("/api/v1/projects/prj_02/comments", json={"body": "love it\nreally"},
                      headers=PARTICIPANT).status_code == 201
        assert c.post("/api/v1/votes", json={"project": "prj_02"}, headers=PARTICIPANT).status_code == 201
        uid = _db(a).execute("SELECT id FROM users WHERE email = 'priya1@example.org'").fetchone()[0]
        assert c.post(f"/api/v1/voters/{uid}/void", json={"reason": "test"}, headers=ORGANIZER).status_code == 303
        assert c.post("/api/v1/event", json={"voting_close": "2026-02-01T00:00:00Z"},
                      headers=ORGANIZER).status_code == 303                    # close: votes are exported
        exported = c.get(f"/api/v1/events/{EVENT_ID}/export.json", headers=ORGANIZER).json()
    for k in ("drafts", "assignments", "comments", "votes", "voided_voters", "organizers"):
        assert exported[k], k
    assert len(exported["assignments"]) > len(exported["scores"])              # pending ones included
    link = _admin_in_empty_portal(b, capsys)
    with empty_portal(b) as c:
        admin = _admin_token(c, link)
        assert c.post("/api/v1/import", json=exported, headers=admin).status_code == 201
        again = c.get(f"/api/v1/events/{EVENT_ID}/export.json", headers=admin).json()
    for k in ("drafts", "assignments", "comments", "votes", "voided_voters", "rubric"):
        strip = (lambda rows: [{kk: vv for kk, vv in r.items() if kk != "at"} for r in rows]) \
            if k == "voided_voters" else (lambda rows: rows)
        assert sorted(map(_key, strip(again[k]))) == sorted(map(_key, strip(exported[k]))), k
    assert set(exported["organizers"]) <= set(again["organizers"])


def test_round_trip_before_any_score_keeps_rubric(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    with portal(a) as c:
        r = c.post("/api/v1/events", json={"name": "Fresh", "submissions_close": "2099-01-01T00:00:00Z",
                                           "tracks": "Web, Hardware"}, headers=ORGANIZER)
        eid = r.headers["location"].split("event=")[1]
        assert c.post(f"/api/v1/rubric?event={eid}", json={"w_functionality": 2, "w_quality": 1, "w_innovation": 3},
                      headers=ORGANIZER).status_code == 303
        exported = c.get(f"/api/v1/events/{eid}/export.json", headers=ORGANIZER).json()
    assert exported["scores"] == []
    link = _admin_in_empty_portal(b, capsys)
    with empty_portal(b) as c:
        admin = _admin_token(c, link)
        assert c.post("/api/v1/import", json=exported, headers=admin).status_code == 201
        again = c.get(f"/api/v1/events/{eid}/export.json", headers=admin).json()
    assert again["rubric"] == exported["rubric"] == [
        {"name": "functionality", "weight": 2.0}, {"name": "quality", "weight": 1.0},
        {"name": "innovation", "weight": 3.0}]


# === signed records ==========================================================================
def _expected_counts():
    d = load_fixture_json()
    canonical_teams = {p["team"] for p in d["projects"]}
    members = {m for t in d["teams"] if t["id"] in canonical_teams for m in t["members"]}
    judges = {s["judge"] for s in d["scores"]}
    jdg24 = {s["project"] for s in d["scores"] if s["judge"] == "jdg_24"}
    return len(members), len(judges), len(jdg24)


def _pub(c):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    r = c.get("/.well-known/dogfood-signing-key.pem")
    assert r.status_code == 200
    return load_pem_public_key(r.content), r.content


def _canon(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _verifies(pub, record, sig_b64):
    from cryptography.exceptions import InvalidSignature
    try:
        pub.verify(base64.b64decode(sig_b64), _canon(record))
        return True
    except InvalidSignature:
        return False


def _issued(data_dir):
    conn = _db(data_dir)
    try:
        return [dict(r) for r in conn.execute("SELECT id, user_id, kind FROM records ORDER BY id")]
    finally:
        conn.close()


def test_records_case2_before_publish_409(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/records/issue", headers=ORGANIZER)
        assert (r.status_code, r.json()["error"]) == (409, "results_not_published")
        assert c.post("/api/v1/records/issue", headers=JUDGE_A).status_code == 403


def test_records_cases_1_3_4_5_6_7_8(tmp_path):
    n_members, n_judges, n_jdg24 = _expected_counts()
    assert (n_judges, n_jdg24) == (30, 11)
    with portal(tmp_path) as c:
        assert c.post("/api/v1/results/publish", json={}, headers=ORGANIZER).status_code == 303
        r = c.post("/api/v1/records/issue", headers=ORGANIZER)
        assert r.status_code == 200 and r.json()["issued"] == n_members + n_judges        # case 1
        assert c.post("/api/v1/records/issue", headers=ORGANIZER).json()["issued"] == 0  # case 3
        rows = _issued(tmp_path)
        assert len(rows) == n_members + n_judges
        assert sum(r["kind"] == "judge" for r in rows) == n_judges
        assert _count(tmp_path, "SELECT COUNT(*) FROM audit_log WHERE action = 'records.issue'") == 2
        assert all(len(r["id"]) == 32 and int(r["id"], 16) >= 0 for r in rows)            # case 8: 128 bits

        pub, pem = _pub(c)
        for row in rows:
            doc = c.get(f"/records/{row['id']}.json").json()
            assert _verifies(pub, doc["record"], doc["signature"]), row                    # case 4
        judge_uid = _db(tmp_path).execute("SELECT user_id FROM judges WHERE id = 'jdg_24'").fetchone()[0]
        rid = next(r["id"] for r in rows if r["user_id"] == judge_uid and r["kind"] == "judge")
        doc = c.get(f"/records/{rid}.json").json()
        rec = doc["record"]
        assert rec["projects_reviewed"] == 11 and rec["judge_id"] == "jdg_24"              # case 7
        assert set(rec) == {"type", "record_id", "event_id", "event_name", "judge_id", "name",
                            "projects_reviewed", "criteria", "issued_at"}
        assert "prj_" not in json.dumps(rec)
        tampered = {**rec, "projects_reviewed": 12}
        assert not _verifies(pub, tampered, doc["signature"])                              # case 5
        assert "valid" in c.get(f"/verify/{rid}").text                                     # case 6
        assert c.get("/verify/" + "0" * 32).status_code == 404
        assert c.get(f"/records/{rid}").status_code == 200

        # case 8: listed on /me for the person, on nobody else's
        assert rid in c.get("/me", headers=JUDGE_A).text
        assert rid not in c.get("/me", headers=PARTICIPANT).text

        # case 4 with the openssl CLI, exactly as the README documents
        if shutil.which("openssl"):
            (tmp_path / "key.pem").write_bytes(pem)
            (tmp_path / "canonical.json").write_bytes(_canon(rec))
            (tmp_path / "sig.bin").write_bytes(base64.b64decode(doc["signature"]))
            cmd = ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", "key.pem", "-rawin",
                   "-in", "canonical.json", "-sigfile", "sig.bin"]
            out = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True)
            assert "Signature Verified Successfully" in out.stdout, out
            (tmp_path / "canonical.json").write_bytes(_canon(tampered))
            out = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True)
            assert "Signature Verified Successfully" not in out.stdout


def test_records_never_carry_an_email_address(tmp_path):
    # Record pages are public and shareable: a member without a display name gets the part of
    # the address before the @, never the address itself.
    with portal(tmp_path) as c:
        c.post("/api/v1/results/publish", json={}, headers=ORGANIZER)
        c.post("/api/v1/records/issue", headers=ORGANIZER)
        conn = _db(tmp_path)
        payloads = [r[0] for r in conn.execute("SELECT payload FROM records")]
        conn.close()
        assert payloads and not any("@" in json.loads(p)["name"] for p in payloads)
        assert any(json.loads(p)["name"] == "member1_1" for p in payloads)


def test_records_revocation(tmp_path):
    with portal(tmp_path) as c:
        c.post("/api/v1/results/publish", json={}, headers=ORGANIZER)
        c.post("/api/v1/records/issue", headers=ORGANIZER)
        rid = _issued(tmp_path)[0]["id"]
        url = f"/api/v1/records/{rid}/revoke"
        assert c.post(url, json={"reason": "x"}).status_code == 401
        assert c.post(url, json={"reason": "x"}, headers=JUDGE_A).status_code == 403
        assert c.post(url, json={"reason": " "}, headers=ORGANIZER).status_code == 422
        assert c.post(url, json={"reason": "issued to the wrong team"}, headers=ORGANIZER).status_code == 204
        assert c.post(url, json={"reason": "again"}, headers=ORGANIZER).status_code == 409
        doc = c.get(f"/records/{rid}.json").json()
        assert doc["revoked"]["reason"] == "issued to the wrong team"
        pub, _ = _pub(c)
        assert _verifies(pub, doc["record"], doc["signature"])          # genuine, but ...
        assert "REVOKED" in c.get(f"/verify/{rid}").text                 # ... the portal says revoked
        assert "Revoked" in c.get(f"/records/{rid}").text
        assert _count(tmp_path, "SELECT COUNT(*) FROM audit_log WHERE action = 'record.revoke'") == 1


def test_records_edge_non_ascii_names_sign_and_verify(tmp_path):
    from dogfood.core import records
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    rec = {"type": "participant", "name": "Zoë Łukasiewicz 李", "team": "Ñandú"}
    sig = records.sign(key, rec)
    assert _verifies(key.public_key(), rec, sig)
    assert "Zoë".encode("utf-8") in _canon(rec)                                   # UTF-8, not \\u escapes


def test_records_case9_key_missing_regenerated_old_records_fail(tmp_path, capfd):
    with portal(tmp_path) as c:
        c.post("/api/v1/results/publish", json={}, headers=ORGANIZER)
        c.post("/api/v1/records/issue", headers=ORGANIZER)
        rid = _issued(tmp_path)[0]["id"]
        doc = c.get(f"/records/{rid}.json").json()
        old_pub, _ = _pub(c)
    assert _verifies(old_pub, doc["record"], doc["signature"])
    key = tmp_path / "signing_key"
    assert key.stat().st_mode & 0o777 == 0o600
    key.unlink()
    capfd.readouterr()
    with portal(tmp_path) as c:
        new_pub, _ = _pub(c)
        assert not _verifies(new_pub, doc["record"], doc["signature"])
        assert "INVALID" in c.get(f"/verify/{rid}").text
    assert "WARNING" in capfd.readouterr().err


# === widget and framing =======================================================================
def test_widget_cases_10_11_13(tmp_path):
    with portal(tmp_path) as c:
        r = c.get(f"/embed/gallery?event={EVENT_ID}")
        assert r.status_code == 200
        assert r.headers["content-security-policy"] == "frame-ancestors *"
        assert "x-frame-options" not in r.headers
        page_one = c.get("/api/v1/projects").json()["items"]
        for p in page_one:
            assert f'/projects/{p["id"]}"' in r.text
        low = r.text.lower()
        for leak in ("@example.org", "jdg_", "score", "vote"):
            assert leak not in low, leak
        assert "<script" not in low and 'src="http' not in low and "<link" not in low       # no external loads
        for path in ("/projects", "/results", "/login", "/vote", "/me"):
            h = c.get(path, headers=PARTICIPANT).headers
            assert h["x-frame-options"] == "DENY" and h["content-security-policy"] == "frame-ancestors 'none'", path


def test_widget_case12_script(tmp_path):
    with portal(tmp_path) as c:
        r = c.get("/widget.js")
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
        js = r.text
        assert "iframe" in js and "/embed/gallery" in js
        for banned in ("document.cookie", "fetch(", "XMLHttpRequest", "localStorage"):
            assert banned not in js


# === OpenAPI (case 1: "with request/response schemas") ==========================================
BODYLESS = {("post", "/api/v1/teams/leave"), ("post", "/api/v1/teams/invite"), ("post", "/api/v1/join/{code}"),
            ("post", "/api/v1/judges/{judge_id}/include"), ("post", "/api/v1/voters/{user_id}/unvoid"),
            ("post", "/api/v1/records/issue")}


def test_openapi_documents_every_v1_body_and_outcome(tmp_path):
    from dogfood.openapi_docs import OPS
    with portal(tmp_path) as c:
        spec = c.get("/openapi.json").json()
    ops = {(m, p): op for p, item in spec["paths"].items() if p.startswith("/api/v1") for m, op in item.items()}
    assert set(OPS) <= set(ops), set(OPS) - set(ops)                      # no docs for vanished routes
    for (m, p), op in ops.items():
        if m in ("post", "put") and (m, p) not in BODYLESS:
            assert "requestBody" in op, (m, p)
        assert any(s.startswith("2") or s == "303" for s in op["responses"]), (m, p)
        assert all(isinstance(r["description"], str) for r in op["responses"].values()), (m, p)
    assert [x["name"] for x in ops[("post", "/api/v1/votes")]["parameters"]] == ["event"]
    vote = ops[("post", "/api/v1/votes")]["requestBody"]["content"]["application/json"]["schema"]
    assert vote["required"] == ["project"]
    assert "bearer" in spec["components"]["securitySchemes"]


def test_token_last_used_is_recorded(tmp_path):
    with portal(tmp_path) as c:
        tok = c.post("/api/v1/tokens", json={"name": "ci"}, headers=ORGANIZER).json()["token"]
        assert c.get("/api/v1/tokens", headers=ORGANIZER).json()[0]["last_used_at"] is None
        c.get("/api/v1/projects", headers={"Authorization": f"Bearer {tok}"})
        assert c.get("/api/v1/tokens", headers=ORGANIZER).json()[0]["last_used_at"]


def test_housekeeping_prunes_expired_sessions_and_links(tmp_path):
    from dogfood import db
    from dogfood.webhook_worker import Worker
    with portal(tmp_path) as c:
        c.post("/register", data={"email": "old@example.org", "password": "a-long-password"})
        c.post("/api/v1/judges", json={"email": "newjudge@example.org"}, headers=ORGANIZER)
    conn = db.connect(str(tmp_path / "dogfood.db"))
    live = lambda: conn.execute("SELECT COUNT(*) FROM sessions WHERE expires_at IS NOT NULL").fetchone()[0]
    links = lambda: conn.execute("SELECT COUNT(*) FROM password_links").fetchone()[0]
    assert live() >= 1 and links() >= 1
    tokens_before = conn.execute("SELECT COUNT(*) FROM sessions WHERE expires_at IS NULL").fetchone()[0]
    Worker(str(tmp_path / "dogfood.db")).tick(conn, datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert live() == 0 and links() == 0
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE expires_at IS NULL").fetchone()[0] == tokens_before
    conn.close()
