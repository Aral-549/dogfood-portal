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
