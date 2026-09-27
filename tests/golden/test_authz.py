"""contracts/authz.md: pure decisions (dogfood.core.authz) and the same cases over HTTP.

Actors for the pure tests are built by hand. HTTP tests boot the real app on a temp
data dir with DOGFOOD_DEMO_SESSIONS=1 (the demo identities from contracts/acceptance.md).
"""

import pytest

from conftest import (EVENT_ID, JUDGE_A, JUDGE_B, ORGANIZER, PARTICIPANT, load_fixture_json, portal)
from dogfood.core import authz
from dogfood.core.authz import Actor, Decision

VISITOR = Actor()
PART = Actor(user_id="u_part", team_of={EVENT_ID: "tm_01"})
ORG = Actor(user_id="u_org", organizer_of=frozenset({EVENT_ID}))
J24 = Actor(user_id="u_j24", judge_of={EVENT_ID: "jdg_24"})
J26 = Actor(user_id="u_j26", judge_of={EVENT_ID: "jdg_26"})


# --- pure ---------------------------------------------------------------------------
def test_case2_visitor_own_scores_401():
    assert authz.read_own_scores(VISITOR, EVENT_ID) is Decision.UNAUTHENTICATED


def test_case3_participant_own_scores_403():
    assert authz.read_own_scores(PART, EVENT_ID) is Decision.FORBIDDEN


def test_case4_organizer_not_judge_own_scores_403():
    assert authz.read_own_scores(ORG, EVENT_ID) is Decision.FORBIDDEN


def test_case5_judge_own_scores_allow():
    assert authz.read_own_scores(J24, EVENT_ID) is Decision.ALLOW


def test_case7_judge_reads_self_by_id_allow():
    assert authz.read_judge_scores(J24, EVENT_ID, "jdg_24", True) is Decision.ALLOW


def test_case8_peer_by_id_403():
    assert authz.read_judge_scores(J26, EVENT_ID, "jdg_24", True) is Decision.FORBIDDEN


def test_case9_nonexistent_judge_403_same_as_peer():
    assert authz.read_judge_scores(J26, EVENT_ID, "jdg_999", False) is Decision.FORBIDDEN


def test_case10_organizer_reads_judge_allow():
    assert authz.read_judge_scores(ORG, EVENT_ID, "jdg_24", True) is Decision.ALLOW


@pytest.mark.parametrize("actor,expected", [
    (PART, Decision.FORBIDDEN),          # case 11
    (J24, Decision.FORBIDDEN),           # case 12
    (ORG, Decision.ALLOW),               # case 13
    (VISITOR, Decision.UNAUTHENTICATED), # case 15
])
def test_cases_11_12_13_15_export(actor, expected):
    assert authz.export_results(actor, EVENT_ID) is expected


@pytest.mark.parametrize("target", ["JDG_24", "jdg_24 ", " jdg_24", "jdg_24/..", "jdg_24%2F.."])
def test_edge_judge_id_exact_match(target):
    # jdg_24 asking for a near-miss spelling of itself is not "itself"; the id does not exist.
    assert authz.read_judge_scores(J24, EVENT_ID, target, False) is Decision.FORBIDDEN


def test_edge_bearer_wins_over_cookie():
    assert authz.extract_token("Bearer tok_b", "tok_c") == ("tok_b", "bearer")


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "Basic abc"])
def test_edge_bad_headers_give_no_bearer_token(header):
    token, source = authz.extract_token(header, None)
    assert token is None and source is None


def test_edge_token_with_trailing_space_is_not_the_token():
    # Contract edge: "token with trailing space ... all visitor -> 401".
    token, _ = authz.extract_token("Bearer demo-judge-a ", None)
    assert token != "demo-judge-a"


def test_edge_judge_who_is_participant_keeps_both_roles_only_on_own_team():
    both = Actor(user_id="u", judge_of={EVENT_ID: "jdg_24"}, team_of={EVENT_ID: "tm_05"})
    assert authz.read_own_scores(both, EVENT_ID) is Decision.ALLOW
    assert authz.submit_project(both, EVENT_ID, True) == (Decision.ALLOW, None)
    assert authz.edit_project(both, EVENT_ID, "tm_05", True)[0] is Decision.ALLOW
    assert authz.edit_project(both, EVENT_ID, "tm_01", True)[0] is Decision.FORBIDDEN


# --- HTTP ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with portal(tmp_path_factory.mktemp("authz_http")) as c:
        yield c


def _fixture_score_count(judge_id: str) -> int:
    return sum(1 for s in load_fixture_json()["scores"] if s["judge"] == judge_id)


def test_http_case1_visitor_gallery(client):
    assert client.get("/projects").status_code == 200


def test_http_case2_visitor_own_scores(client):
    assert client.get("/api/judge/scores").status_code == 401


def test_http_case3_participant_own_scores(client):
    assert client.get("/api/judge/scores", headers=PARTICIPANT).status_code == 403


def test_http_case4_organizer_own_scores(client):
    # The demo organizer is not a judge.
    assert client.get("/api/judge/scores", headers=ORGANIZER).status_code == 403


def test_http_case5_judge_own_scores_filtered(client):
    r = client.get("/api/judge/scores", headers=JUDGE_A)
    assert r.status_code == 200
    rows = r.json()["scores"]
    assert len(rows) == _fixture_score_count("jdg_24") == 11
    assert {row["judge"] for row in rows} == {"jdg_24"}


@pytest.mark.parametrize("qs", ["?judge=jdg_26", "?judge_id=jdg_26", "?judge=jdg_26&role=organizer"])
def test_http_case6_query_param_ignored(client, qs):
    r = client.get("/api/judge/scores" + qs, headers=JUDGE_A)
    assert r.status_code == 200
    rows = r.json()["scores"]
    assert {row["judge"] for row in rows} == {"jdg_24"}
    assert len(rows) == 11


def test_http_case7_judge_self_by_id(client):
    r = client.get("/api/judges/jdg_24/scores", headers=JUDGE_A)
    assert r.status_code == 200
    assert len(r.json()["scores"]) == 11


def test_http_case8_peer_by_id(client):
    assert client.get("/api/judges/jdg_24/scores", headers=JUDGE_B).status_code == 403


def test_http_case9_nonexistent_by_id(client):
    assert client.get("/api/judges/jdg_999/scores", headers=JUDGE_B).status_code == 403


def test_http_case10_organizer_by_id(client):
    r = client.get("/api/judges/jdg_24/scores", headers=ORGANIZER)
    assert r.status_code == 200
    assert {row["judge"] for row in r.json()["scores"]} == {"jdg_24"}
    assert len(r.json()["scores"]) == 11


@pytest.mark.parametrize("headers,expected", [
    (PARTICIPANT, 403),  # case 11
    (JUDGE_A, 403),      # case 12
    (ORGANIZER, 200),    # case 13
    ({}, 401),           # case 15
])
def test_http_export(client, headers, expected):
    assert client.get("/api/export.csv", headers=headers).status_code == expected


def test_http_case16_no_side_door_by_score_row_id(client):
    # There is no route for a single score row. Probe plausible by-id URLs as jdg_24
    # with jdg_26's review ids (1..126 cover every fixture review) and require that
    # none of them returns jdg_26's data.
    for rid in range(1, 130):
        for path in (f"/api/judge/scores/{rid}", f"/api/scores/{rid}", f"/api/reviews/{rid}"):
            r = client.get(path, headers=JUDGE_A)
            assert r.status_code != 200 or "jdg_26" not in r.text, path


@pytest.mark.parametrize("path", [
    "/api/judges/JDG_24/scores", "/api/judges/jdg_24%20/scores", "/api/judges/%20jdg_24/scores",
    "/api/judges/jdg_24%2F../scores", "/api/judges/jdg_26/../jdg_24/scores",
    "/api/judges/jdg_24/scores/", "/api/judges/jdg_24/scores?judge=jdg_26",
])
def test_http_edge_id_variants_never_leak(client, path):
    r = client.get(path, headers=JUDGE_B)
    assert r.status_code != 200 or '"jdg_24"' not in r.text


@pytest.mark.parametrize("value", ["", "Bearer", "Bearer ", "Bearer nope", "Token demo-judge-a"])
def test_http_edge_bad_auth_is_visitor(client, value):
    assert client.get("/api/judge/scores", headers={"Authorization": value}).status_code == 401


def test_http_edge_trailing_space_token_is_visitor(client):
    r = client.get("/api/judge/scores", headers={"Authorization": "Bearer demo-judge-a "})
    assert r.status_code == 401


def test_http_edge_bearer_wins_over_cookie(client):
    r = client.get("/api/judge/scores", headers={**JUDGE_B, "Cookie": "session=demo-judge-a"})
    assert r.status_code == 200
    assert r.json()["judge"] == "jdg_26"
    r = client.get("/api/judges/jdg_24/scores", headers={**JUDGE_B, "Cookie": "session=demo-judge-a"})
    assert r.status_code == 403


def test_http_role_not_taken_from_client(client):
    for h in ({"X-Role": "organizer"}, {"X-Role": "admin"}):
        assert client.get("/api/export.csv", headers={**JUDGE_B, **h}).status_code == 403
    assert client.get("/api/export.csv?role=organizer", headers=JUDGE_B).status_code == 403


@pytest.mark.parametrize("headers", [JUDGE_A, JUDGE_B, PARTICIPANT, {}])
def test_http_non_organizer_pages_hold_no_peer_scores(client, headers):
    # The organizer dashboard is the only HTML with every judge's work; nobody else gets it.
    r = client.get("/organizer", headers=headers)
    assert r.status_code in (401, 403, 303)
    assert "Raw mean" not in r.text
