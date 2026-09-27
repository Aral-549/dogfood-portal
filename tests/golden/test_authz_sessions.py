"""contracts/authz.md case 14 and session edge cases, each on its own fresh portal.

Kept apart from test_authz.py so no two TestClient lifespans are open at once.
"""

from conftest import JUDGE_A, JUDGE_B, ORGANIZER, PARTICIPANT, load_fixture_json, portal


def test_http_case14_demo_tokens_absent_without_flag(tmp_path):
    with portal(tmp_path, demo=False) as c:
        for h in (ORGANIZER, JUDGE_A, JUDGE_B, PARTICIPANT):
            assert c.get("/api/judge/scores", headers=h).status_code == 401
            assert c.get("/api/export.csv", headers=h).status_code == 401


def test_http_case14_demo_tokens_revoked_when_flag_removed(tmp_path):
    with portal(tmp_path, demo=True) as c:
        assert c.get("/api/export.csv", headers=ORGANIZER).status_code == 200
    with portal(tmp_path, demo=False) as c:
        assert c.get("/api/export.csv", headers=ORGANIZER).status_code == 401


def test_http_logout_revokes_session(tmp_path):
    with portal(tmp_path, demo=True) as c:
        r = c.post("/login", data={"email": "diego.herrera@example.org", "password": "dogfood-demo"})
        assert r.status_code == 303
        tok = r.cookies.get("session")
        assert tok
        cookie = {"Cookie": f"session={tok}"}
        assert c.get("/api/judge/scores", headers=cookie).status_code == 200
        assert c.post("/logout", headers=cookie).status_code == 303
        assert c.get("/api/judge/scores", headers=cookie).status_code == 401


def test_http_csrf_cross_origin_cookie_post_blocked(tmp_path):
    # lifecycle.md case 21, the CSRF defence the authz stage relies on for cookie sessions.
    with portal(tmp_path, demo=True) as c:
        r = c.post("/login", data={"email": "diego.herrera@example.org", "password": "dogfood-demo"})
        cookie = {"Cookie": f"session={r.cookies.get('session')}"}
        form = {"functionality": "1", "quality": "1", "innovation": "1"}
        for origin in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                       {"Referer": "http://evil.example/x"}):
            r = c.post("/judge/projects/prj_06", data=form, headers={**cookie, **origin})
            assert r.status_code == 403
        # jdg_24's own prj_06 review is unchanged by the refused posts: read it back.
        own = c.get("/api/judge/scores", headers=JUDGE_A).json()["scores"]
        fixture = next(s for s in load_fixture_json()["scores"]
                       if s["judge"] == "jdg_24" and s["project"] == "prj_06")
        got = next(s for s in own if s["project"] == "prj_06")
        assert got["criteria"] == fixture["criteria"]
