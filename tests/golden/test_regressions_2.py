"""Regression cases for BUGLOG.md BUG-10 .. BUG-14 (second verification pass, 2026-09-27).

Written from the BUGLOG symptom text and the contracts, not from the code's output.
No mocks or monkeypatching: every test boots the real app through conftest.portal()
(or the same TestClient path with a different fixture file) and talks HTTP to it.
Log assertions attach an extra handler to the "dogfood" logger through the standard
logging API after boot; nothing in the code under test is replaced.
A test that fails here means the implementation is wrong: keep it failing, fix the code.
"""

import json
import logging
from contextlib import contextmanager

import pytest

from conftest import (FIXTURES_PATH, JUDGE_A, JUDGE_B, ORGANIZER, PARTICIPANT, env,
                      load_fixture_json, portal)

JSON = {"Accept": "application/json"}
FUTURE_CLOSE = "2099-01-01T00:00:00Z"
ALL_THREES = {"functionality": 3, "quality": 3, "innovation": 3}


@contextmanager
def captured_log_records():
    """Collect the portal's structured log lines (one JSON object per line, logs.py)."""
    records: list[dict] = []

    class Collect(logging.Handler):
        def emit(self, record):
            try:
                records.append(json.loads(record.getMessage()))
            except ValueError:
                pass

    handler = Collect(level=logging.DEBUG)
    logger = logging.getLogger("dogfood")
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def open_submissions(client):
    """lifecycle.md case 12: the organizer moves submissions_close; the only way to reopen."""
    r = client.post("/organizer/event", json={"submissions_close": FUTURE_CLOSE}, headers=ORGANIZER)
    assert r.status_code == 303, r.text


# --- BUG-10: GET /projects/new must not be routed to /projects/{project_id} ----------------

def test_bug10_projects_new_is_not_treated_as_a_project_id(tmp_path):
    # BUGLOG BUG-10 symptom: the submit form route "would have been treated as a project id (404)".
    with portal(tmp_path) as c:
        r = c.get("/projects/new", headers=PARTICIPANT)
        assert r.status_code != 404
        assert "No such project" not in r.text


def test_bug10_projects_new_renders_the_submit_form_while_open(tmp_path):
    # submissions.md case 1 actor (participant in tm_01) before the close: the form is shown.
    with portal(tmp_path) as c:
        open_submissions(c)
        r = c.get("/projects/new", headers=PARTICIPANT)
        assert r.status_code == 200
        assert "No such project" not in r.text
        assert 'name="title"' in r.text


# --- BUG-11: GET /projects/new after the close shows the refusal, not the form -------------

def test_bug11_projects_new_after_close_is_refused_json(tmp_path):
    # submissions.md case 4: real clock 2026-09-27, fixture closed 2026-03-01 -> submissions_closed.
    # lifecycle.md edge: "GET /projects/new after the close shows the refusal, not the form".
    with portal(tmp_path) as c:
        r = c.get("/projects/new", headers={**PARTICIPANT, **JSON})
        assert r.status_code == 403
        assert r.json() == {"error": "submissions_closed"}


def test_bug11_projects_new_after_close_html_has_no_form(tmp_path):
    with portal(tmp_path) as c:
        r = c.get("/projects/new", headers=PARTICIPANT)
        assert r.status_code == 403
        assert "submissions_closed" in r.text
        assert 'name="title"' not in r.text


# --- BUG-12: a judge who is a member of the project's team cannot score it -----------------

def test_bug12_judge_on_projects_team_gets_conflict_of_interest(tmp_path):
    # lifecycle.md case 17a: 403 conflict_of_interest, re-checked at scoring time (not only at
    # assignment). fixtures-import.md out-of-scope note: a judge with a fixture score on a project
    # is assigned to it; upstream fixtures.json has a jdg_24 score on prj_06 (team tm_06, 1 member).
    fixture = load_fixture_json()
    assert any(s["judge"] == "jdg_24" and s["project"] == "prj_06" for s in fixture["scores"])
    assert next(p for p in fixture["projects"] if p["id"] == "prj_06")["team"] == "tm_06"
    with portal(tmp_path) as c:
        open_submissions(c)  # joining a team needs submissions open (lifecycle.md case 8)
        # Control: before joining, jdg_24 may score its assigned project (lifecycle.md case 16).
        r = c.post("/api/judge/scores/prj_06", json=ALL_THREES, headers=JUDGE_A)
        assert r.status_code == 200, r.text
        # The team's invite link is /join/<invite_code> (lifecycle.md case 6). tm_06's only member
        # has no password, so the code is read (read-only) from the teams table.
        code = c.app.state.conn.execute("SELECT invite_code FROM teams WHERE id = 'tm_06'").fetchone()[0]
        r = c.post(f"/join/{code}", headers=JUDGE_A)
        assert r.status_code == 303, r.text
        me = c.get("/me", headers=JUDGE_A)
        assert me.status_code == 200 and "/join/" + code in me.text  # jdg_24 is now in tm_06
        r = c.post("/api/judge/scores/prj_06", json={"functionality": 5, "quality": 5, "innovation": 5},
                   headers=JUDGE_A)
        assert r.status_code == 403
        assert r.json() == {"error": "conflict_of_interest"}
        # The refused request wrote nothing: jdg_24's stored score is still the control's 3,3,3.
        mine = {s["project"]: s["criteria"] for s in c.get("/api/judge/scores", headers=JUDGE_A).json()["scores"]}
        assert mine["prj_06"] == ALL_THREES


def test_bug12_html_form_route_also_refuses_conflict(tmp_path):
    # The same rule on the HTML form route (POST /judge/projects/{id}); lifecycle.md case 17a.
    with portal(tmp_path) as c:
        open_submissions(c)
        code = c.app.state.conn.execute("SELECT invite_code FROM teams WHERE id = 'tm_06'").fetchone()[0]
        assert c.post(f"/join/{code}", headers=JUDGE_A).status_code == 303
        r = c.post("/judge/projects/prj_06", data=ALL_THREES, headers={**JUDGE_A, **JSON})
        assert r.status_code == 403
        assert r.json() == {"error": "conflict_of_interest"}


# --- BUG-13: POST /logout with a Bearer token must not revoke the shared demo token --------

def test_bug13_logout_with_demo_bearer_does_not_revoke_it(tmp_path):
    # lifecycle.md edge: "POST /logout revokes only the caller's cookie session, never a Bearer token".
    with portal(tmp_path) as c:
        assert c.get("/api/judge/scores", headers=JUDGE_A).status_code == 200
        c.post("/logout", headers=JUDGE_A)
        r = c.get("/api/judge/scores", headers=JUDGE_A)
        assert r.status_code == 200
        assert r.json()["judge"] == "jdg_24"


def test_bug13_logout_with_demo_token_as_cookie_does_not_revoke_it(tmp_path):
    # BUGLOG BUG-13 symptom: logout "deleted that session for every user of the token until
    # restart". acceptance.md fixes demo-judge-b as a shared Bearer token for the checker; the
    # lifecycle.md edge says a Bearer token is never revoked by logout. Presenting the same
    # shared token in the session cookie must not let one caller revoke it for everyone.
    with portal(tmp_path) as c:
        c.cookies.set("session", "demo-judge-b")
        c.post("/logout")
        c.cookies.clear()
        r = c.get("/api/judge/scores", headers=JUDGE_B)
        assert r.status_code == 200
        assert r.json()["judge"] == "jdg_26"


def test_bug13_cookie_logout_still_revokes_the_cookie_session(tmp_path):
    # lifecycle.md case 3: "session row deleted; old token -> visitor" (the fix must keep this).
    with portal(tmp_path) as c:
        r = c.post("/login", data={"email": "diego.herrera@example.org", "password": "dogfood-demo"})
        assert r.status_code == 303
        token = r.cookies.get("session")
        assert token
        assert c.post("/logout").status_code == 303
        c.cookies.clear()
        assert c.get("/api/judge/scores", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# --- BUG-14: minor contract gaps ------------------------------------------------------------

def test_bug14a_bearer_and_cookie_for_different_users_bearer_wins_and_is_logged(tmp_path):
    # authz.md edge: "Both cookie and Bearer present and pointing at different users: Bearer wins,
    # and it is logged."
    with portal(tmp_path) as c:
        with captured_log_records() as records:
            c.cookies.set("session", "demo-judge-b")
            r = c.get("/api/judge/scores", headers=JUDGE_A)
            c.cookies.clear()
        assert r.status_code == 200 and r.json()["judge"] == "jdg_24"
        assert any(rec.get("stage") == "auth" and rec.get("event") == "credential_conflict" for rec in records)


def test_bug14b_merged_duplicate_reviews_are_counted_in_a_warning(tmp_path):
    # scoring.md edge: "A review whose project is not in projects (superseded duplicate): ignored,
    # and counted in a warning." fixtures-import.md case 7: jdg_19, jdg_21 and jdg_26 scored both
    # prj_07 and prj_41 and count once, so 3 raw reviews are not counted.
    with portal(tmp_path) as c:
        with captured_log_records() as records:
            r = c.get("/api/export.csv", headers=ORGANIZER)
        assert r.status_code == 200
        warnings = [rec for rec in records
                    if rec.get("stage") == "scoring" and rec.get("event") == "reviews_not_counted"]
        assert warnings
        assert warnings[-1].get("merged_duplicates") == 3


@pytest.mark.parametrize("variant", ["missing", "null"])
def test_bug14c_fixture_without_event_fails_boot_loudly(tmp_path, variant):
    # fixtures-import.md edge: an unusable file makes "boot fail loudly with the path and reason;
    # portal does not start half-seeded". BUG-14(c): without 'event' it booted an empty portal.
    from fastapi.testclient import TestClient
    from dogfood.app import app
    from dogfood.core.importer import FixtureError

    data = load_fixture_json()
    if variant == "missing":
        del data["event"]
    else:
        data["event"] = None
    bad = tmp_path / "no_event.json"
    bad.write_text(json.dumps(data))
    data_dir = tmp_path / "data"
    with env(DOGFOOD_DATA=str(data_dir), DOGFOOD_FIXTURES=str(bad), DOGFOOD_DEMO_SESSIONS="1"):
        with pytest.raises(FixtureError) as info:
            with TestClient(app, raise_server_exceptions=False):
                pass
    message = str(info.value)
    assert "event" in message
    assert str(bad) in message  # "with the path and reason"


def test_bug14c_upstream_fixture_still_boots(tmp_path):
    # Control for the case above: the real upstream file (with its event) boots and serves.
    assert FIXTURES_PATH.exists()
    with portal(tmp_path, demo=False) as c:
        assert c.get("/projects").status_code == 200


def test_bug14d_docs_is_not_a_cdn_swagger_page(tmp_path):
    # BUG-14(d): /docs loaded Swagger UI from a CDN (offline rule). Docs UI disabled.
    with portal(tmp_path) as c:
        for path in ("/docs", "/redoc"):
            r = c.get(path)
            body = r.text.lower()
            assert not (r.status_code == 200 and ("swagger" in body or "redoc" in body)), path
            assert "cdn.jsdelivr" not in body and "unpkg.com" not in body and "cdn." not in body, path


def test_bug14d_openapi_json_still_served(tmp_path):
    # BUG-14 root cause text: "docs UI disabled (/openapi.json remains)".
    with portal(tmp_path) as c:
        r = c.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert "openapi" in spec
        assert "/api/projects" in spec["paths"]
