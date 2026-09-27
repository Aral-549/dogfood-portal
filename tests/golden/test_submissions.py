"""contracts/submissions.md: deadline rule with injected times, the authz ordering, and
case 4 (plus the other after-close cases) over HTTP on the real clock.

Anchor: CLOSE = 2026-03-01T18:00:00Z (fixture event). Times are CLOSE +/- 1 second.
The real clock (2026-09 or later) is after CLOSE, which is what contract case 4 relies on.
"""

import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from conftest import CLOSE, EVENT_ID, JUDGE_A, ONE_SECOND, ORGANIZER, PARTICIPANT, env, portal
from dogfood.core import authz
from dogfood.core.authz import Actor, Decision
from dogfood.core.deadline import submissions_open
from dogfood.core.timeutil import parse_utc

VISITOR = Actor()
TM01 = Actor(user_id="u1", team_of={EVENT_ID: "tm_01"})
TM02 = Actor(user_id="u2", team_of={EVENT_ID: "tm_02"})
JUDGE_NO_TEAM = Actor(user_id="u3", judge_of={EVENT_ID: "jdg_24"})
NO_TEAM = Actor(user_id="u4")
ORG = Actor(user_id="u5", organizer_of=frozenset({EVENT_ID}))


def is_open(now):
    return submissions_open(CLOSE, now)


# --- rule -----------------------------------------------------------------------------
def test_case1_one_second_before_close_is_open():
    assert is_open(CLOSE - ONE_SECOND) is True
    assert authz.submit_project(TM01, EVENT_ID, is_open(CLOSE - ONE_SECOND)) == (Decision.ALLOW, None)


def test_case2_exactly_at_close_is_closed():
    assert is_open(CLOSE) is False
    assert authz.submit_project(TM01, EVENT_ID, is_open(CLOSE)) == (Decision.FORBIDDEN, "submissions_closed")


def test_case3_one_second_after_close_is_closed():
    assert is_open(CLOSE + ONE_SECOND) is False
    assert authz.submit_project(TM01, EVENT_ID, is_open(CLOSE + ONE_SECOND)) == \
        (Decision.FORBIDDEN, "submissions_closed")


def test_one_microsecond_before_close_is_open():
    assert is_open(CLOSE - timedelta(microseconds=1)) is True


def test_case5_deadline_checked_before_role_and_body():
    # After close, the reason is submissions_closed whoever asks (validation runs later).
    for actor in (TM01, JUDGE_NO_TEAM, NO_TEAM, ORG):
        assert authz.submit_project(actor, EVENT_ID, is_open(CLOSE + ONE_SECOND)) == \
            (Decision.FORBIDDEN, "submissions_closed")


def test_case6_visitor_401_before_close():
    decision, _ = authz.submit_project(VISITOR, EVENT_ID, is_open(CLOSE - ONE_SECOND))
    assert decision is Decision.UNAUTHENTICATED


def test_case7_judge_without_team():
    assert authz.submit_project(JUDGE_NO_TEAM, EVENT_ID, is_open(CLOSE - ONE_SECOND)) == \
        (Decision.FORBIDDEN, "not_a_participant")


def test_case8_participant_without_team():
    assert authz.submit_project(NO_TEAM, EVENT_ID, is_open(CLOSE - ONE_SECOND)) == \
        (Decision.FORBIDDEN, "no_team")


def test_case10_edit_own_before_close():
    assert authz.edit_project(TM01, EVENT_ID, "tm_01", is_open(CLOSE - ONE_SECOND))[0] is Decision.ALLOW


def test_case11_edit_other_team_before_close():
    assert authz.edit_project(TM02, EVENT_ID, "tm_01", is_open(CLOSE - ONE_SECOND))[0] is Decision.FORBIDDEN


def test_case12_edit_own_after_close():
    assert authz.edit_project(TM01, EVENT_ID, "tm_01", is_open(CLOSE + ONE_SECOND)) == \
        (Decision.FORBIDDEN, "submissions_closed")


def test_case14_organizer_edit_after_close():
    assert authz.edit_project(ORG, EVENT_ID, "tm_01", is_open(CLOSE + ONE_SECOND))[0] is Decision.FORBIDDEN


# --- edge cases ------------------------------------------------------------------------
def test_edge_z_and_offset_parse_to_same_instant():
    assert parse_utc("2026-03-01T18:00:00Z") == parse_utc("2026-03-01T18:00:00+00:00") == CLOSE
    # 10:00 at -08:00 is 18:00 UTC.
    assert parse_utc("2026-03-01T10:00:00-08:00") == CLOSE


def test_edge_naive_now_raises():
    with pytest.raises(ValueError):
        submissions_open(CLOSE, datetime(2026, 3, 1, 17, 0, 0))


def test_edge_naive_timestamp_string_rejected():
    with pytest.raises(ValueError):
        parse_utc("2026-03-01T18:00:00")


def test_edge_server_tz_does_not_change_decisions():
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset unavailable on this platform")
    decisions = []
    for tz in ("UTC", "America/Los_Angeles", "Asia/Kolkata"):
        with env(TZ=tz):
            time.tzset()
            decisions.append((is_open(CLOSE - ONE_SECOND), is_open(CLOSE), is_open(CLOSE + ONE_SECOND)))
    time.tzset()
    assert decisions == [(True, False, False)] * 3


# --- HTTP on the real clock (after close) ------------------------------------------------
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    assert datetime.now(timezone.utc) > CLOSE  # precondition for contract case 4
    with portal(tmp_path_factory.mktemp("subm_http")) as c:
        yield c


def test_http_case4_checker_probe_refused_as_closed(client):
    r = client.post("/api/projects", json={"title": "dogfood-late-submission-probe", "summary": "probe"},
                    headers=PARTICIPANT)
    assert r.status_code == 403
    assert r.json() == {"error": "submissions_closed"}


def test_http_case4_no_row_created(client):
    client.post("/api/projects", json={"title": "dogfood-late-submission-probe", "summary": "probe"},
                headers=PARTICIPANT)
    r = client.get("/api/projects", params={"q": "late-submission-probe"})
    assert r.json()["total"] == 0


def test_http_case4_form_body_same_decision(client):
    r = client.post("/api/projects", data={"title": "dogfood-late-submission-probe", "summary": "probe"},
                    headers=PARTICIPANT)
    assert r.status_code == 403
    assert r.json() == {"error": "submissions_closed"}


@pytest.mark.parametrize("body", [{"title": ""}, {"title": "   "}, {"title": "x" * 201}, {}])
def test_http_case5_invalid_body_after_close_still_closed(client, body):
    r = client.post("/api/projects", json=body, headers=PARTICIPANT)
    assert r.status_code == 403
    assert r.json() == {"error": "submissions_closed"}


def test_http_case6_visitor_401(client):
    assert client.post("/api/projects", json={"title": "x"}).status_code == 401


def test_http_case12_edit_own_after_close(client):
    # prj_01 belongs to tm_01 (fixtures.json), the demo participant's team.
    r = client.put("/api/projects/prj_01", json={"title": "late edit"}, headers=PARTICIPANT)
    assert r.status_code == 403
    assert r.json() == {"error": "submissions_closed"}


def test_http_case14_organizer_edit_after_close(client):
    r = client.put("/api/projects/prj_01", json={"title": "late edit"}, headers=ORGANIZER)
    assert r.status_code == 403


def test_http_judge_submit_after_close_reason_is_deadline(client):
    r = client.post("/api/projects", json={"title": "x"}, headers=JUDGE_A)
    assert r.status_code == 403
    assert r.json() == {"error": "submissions_closed"}


def test_http_team_create_and_join_refused_after_close(client):
    # lifecycle.md case 8 (join after close) and the brief's "deadline that actually holds".
    assert client.post("/teams", data={"name": "late"}, headers=JUDGE_A).status_code == 403
    me = client.get("/me", headers=PARTICIPANT)
    codes = re.findall(r"/join/([A-Za-z0-9_-]+)", me.text)
    assert codes, "participant's /me page shows the tm_01 invite link"
    r = client.post(f"/join/{codes[0]}", headers=JUDGE_A)
    assert r.status_code == 403
