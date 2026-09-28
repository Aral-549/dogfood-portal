"""Cross-cutting behaviour of the whole portal (added 2026-09-27): request size limits and
security headers. Runs the real app through TestClient, like the rest of the suite."""

from conftest import ORGANIZER, PARTICIPANT, portal


def test_oversized_body_413_declared(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/projects/prj_02/comments", content=b"x" * (2 << 20),
                   headers={**PARTICIPANT, "Content-Type": "application/json"})
        assert r.status_code == 413 and r.json() == {"error": "body_too_large"}
        assert c.get("/healthz").status_code == 200


def test_oversized_body_413_chunked(tmp_path):
    def chunks():
        for _ in range(40):
            yield b"x" * 65536                      # 2.5 MiB, no Content-Length

    with portal(tmp_path) as c:
        r = c.post("/api/v1/projects/prj_02/comments", content=chunks(),
                   headers={**PARTICIPANT, "Content-Type": "application/json"})
        assert r.status_code == 413


def test_import_allows_large_files(tmp_path):
    with portal(tmp_path) as c:
        big = b'{"event": {"id": "evt_big", "submissions_close": "2026-01-01T00:00:00Z"}, "pad": "' + \
            b"x" * (3 << 20) + b'"}'
        r = c.post("/api/v1/import", content=big, headers={**ORGANIZER, "Content-Type": "application/json"})
        assert r.status_code == 201, r.text[:200]


def test_normal_bodies_unaffected(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/projects/prj_02/comments", json={"body": "fine"}, headers=PARTICIPANT)
        assert r.status_code == 201


def test_security_headers_everywhere(tmp_path):
    with portal(tmp_path) as c:
        for path in ("/projects", "/api/v1/projects", "/embed/gallery", "/login"):
            h = c.get(path).headers
            assert h["x-content-type-options"] == "nosniff", path
            assert h["referrer-policy"] == "same-origin", path


def test_session_cookie_secure_only_over_https(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/register", data={"email": "cookie@example.org", "password": "a-long-password"})
        assert "secure" not in r.headers["set-cookie"].lower()
    from fastapi.testclient import TestClient
    from conftest import env, FIXTURES_PATH
    from dogfood.app import app
    with env(DOGFOOD_DATA=str(tmp_path), DOGFOOD_FIXTURES=str(FIXTURES_PATH)):
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
            r = c.post("/register", data={"email": "cookie2@example.org", "password": "a-long-password"})
            assert "secure" in r.headers["set-cookie"].lower()


def test_event_choice_is_remembered_by_pages_not_by_the_api(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/api/v1/events", json={"name": "Second Hack", "submissions_close": "2099-01-01T00:00:00Z"},
                   headers=ORGANIZER)
        eid = r.headers["location"].split("event=")[1]
        page = c.get("/projects", headers=ORGANIZER).text
        assert "Second Hack" in page and 'name="event"' in page             # switcher shown: 2 events
        assert c.get(f"/projects?event={eid}").status_code == 200           # pick it ...
        assert "Second Hack" in c.get("/projects").text.split("<title>")[1].split("</title>")[0]  # ... remembered
        assert c.get("/api/v1/ballot", headers=PARTICIPANT).json()["event"] == "evt_01"          # API: explicit
        assert "No event" in c.get("/projects?event=evt_nope").text          # an explicit unknown id is not guessed
        c.get("/projects?event=evt_01")
        assert "Sample Hack 2026" in c.get("/projects").text.split("</title>")[0]


def test_no_switcher_with_one_event(tmp_path):
    with portal(tmp_path) as c:
        assert 'name="event"' not in c.get("/projects").text


def test_upgrade_from_t2_volume(tmp_path):
    """A volume created by the T2 release (schema frozen in data/schema_t2.sql) boots on today's
    code: columns are added, existing rows and rankings are untouched, new features work."""
    import sqlite3
    from pathlib import Path
    old = (Path(__file__).parent / "data" / "schema_t2.sql").read_text()
    conn = sqlite3.connect(str(tmp_path / "dogfood.db"))
    conn.executescript(old)
    conn.close()
    with portal(tmp_path) as c:                     # boot imports the fixtures into the old-shape file
        before = c.get("/api/export.csv", headers=ORGANIZER).text
    with portal(tmp_path) as c:                     # a second boot on the migrated file
        assert c.get("/api/export.csv", headers=ORGANIZER).text == before
        assert c.post("/api/v1/event", json={"voting_open": "2026-01-01T00:00:00Z",
                                             "voting_close": "2099-01-01T00:00:00Z"}, headers=ORGANIZER).status_code == 303
        assert c.post("/api/v1/votes", json={"project": "prj_02"}, headers=PARTICIPANT).status_code == 201
        c.post("/api/v1/results/publish", json={}, headers=ORGANIZER)
        assert c.post("/api/v1/records/issue", headers=ORGANIZER).json()["issued"] > 0
    conn = sqlite3.connect(str(tmp_path / "dogfood.db"))
    cols = {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in ("events", "users", "records", "sessions")}
    conn.close()
    assert {"voting_open", "voting_close", "votes_per_voter", "voting_closed_sent"} <= cols["events"]
    assert "email_norm" in cols["users"] and "revoked_at" in cols["records"] and "last_used_at" in cols["sessions"]


def test_judge_page_never_preselects_a_score(tmp_path):
    import re
    from conftest import JUDGE_A
    with portal(tmp_path) as c:
        assert c.post("/api/v1/assignments/auto", json={"k": 6}, headers=ORGANIZER).status_code == 303
        page = c.get("/judge", headers=JUDGE_A).text
        forms = re.findall(r'<section class="card">.*?</section>', page, re.S)
        todo = [f for f in forms if "to do" in f]
        assert todo, "fixture judge has unscored assignments"
        for f in todo:
            assert f.count('<option value="" selected disabled>-</option>') == 3      # one per criterion
            assert "<option selected>" not in f
        scored = [f for f in forms if ">scored<" in f]
        assert all(f.count("<option selected>") == 3 for f in scored)
        assert page.index("to do") < page.index(">scored<")                            # to do first
        assert re.search(r"\d+ of \d+ scored", page)
        # and an untouched form is refused rather than stored as 1s
        pid = re.search(r'action="/judge/projects/([^"]+)"', todo[0]).group(1)
        assert c.post(f"/judge/projects/{pid}", data={"comment": "oops"}, headers=JUDGE_A).status_code == 422


def test_account_lifecycle_entirely_over_the_api(tmp_path):
    """API First: register, log in, act, log out, with no HTML form and no cookie."""
    with portal(tmp_path) as c:
        r = c.post("/api/v1/register", json={"email": "api.only@example.org", "password": "api-password"})
        assert r.status_code == 201 and r.json()["token_type"] == "bearer"
        assert "set-cookie" not in r.headers
        assert c.post("/api/v1/register", json={"email": "api.only@example.org", "password": "api-password"}
                      ).status_code == 409
        assert c.post("/api/v1/register", json={"email": "x", "password": "short"}).status_code == 422
        assert c.post("/api/v1/login", json={"email": "api.only@example.org", "password": "nope"}).status_code == 401
        r = c.post("/api/v1/login", json={"email": "api.only@example.org", "password": "api-password"})
        assert r.status_code == 200 and "set-cookie" not in r.headers
        bearer = {"Authorization": f"Bearer {r.json()['token']}"}
        assert c.get("/api/v1/tokens", headers=bearer).status_code == 200
        api_tok = c.post("/api/v1/tokens", json={}, headers=bearer).json()["token"]
        assert c.post("/api/v1/logout", headers={"Authorization": f"Bearer {api_tok}"}).status_code == 409
        assert c.post("/api/v1/logout", headers=bearer).status_code == 204
        assert c.get("/api/v1/tokens", headers=bearer).status_code == 401          # session gone
        assert c.get("/api/v1/tokens", headers={"Authorization": f"Bearer {api_tok}"}).status_code == 200


def test_set_password_link_over_the_api(tmp_path):
    with portal(tmp_path) as c:
        loc = c.post("/organizer/judges", data={"email": "api.judge@example.org"}, headers=ORGANIZER).headers["location"]
        link = loc.split("link=", 1)[1]
        token = link.rsplit("/", 1)[1]
        assert c.post(f"/api/v1/set-password/{token}", json={"password": "short"}).status_code == 422
        assert c.post(f"/api/v1/set-password/{token}", json={"password": "judge-password"}).status_code == 204
        assert c.post(f"/api/v1/set-password/{token}", json={"password": "judge-password"}).status_code == 404
        assert c.post("/api/v1/login", json={"email": "api.judge@example.org", "password": "judge-password"}
                      ).status_code == 200
