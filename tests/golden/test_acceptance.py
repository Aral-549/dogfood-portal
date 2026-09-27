"""contracts/acceptance.md cases 1-7: the checker's seven requests, sent the way run.py
sends them, against the real app booted in demo mode on a fresh data dir.
"""

import json

import pytest

from conftest import JUDGE_A, JUDGE_B, ORGANIZER, PARTICIPANT, load_fixture_json, portal


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with portal(tmp_path_factory.mktemp("acceptance")) as c:
        yield c


def test_case1_gallery_public(client):
    assert client.get("/projects").status_code == 200


def test_case2_first_three_fixture_titles_on_page_one(client):
    body = client.get("/projects").text.lower()
    # run.py's fixture_titles(): first 3 project titles of the file.
    titles = [p["title"] for p in load_fixture_json()["projects"][:3]]
    assert titles == ["Glass Signal", "Small Meadow", "Deep Compass"]
    # Contract: at least one; all three are expected on page one (gallery.md case 1).
    assert any(t.lower() in body for t in titles)
    assert all(t.lower() in body for t in titles)


def test_case3_late_submission_refused_for_the_deadline(client):
    r = client.post("/api/projects",
                    content=json.dumps({"title": "dogfood-late-submission-probe", "summary": "probe"}).encode(),
                    headers={**PARTICIPANT, "Content-Type": "application/json"})
    assert r.status_code == 403
    assert r.json().get("error") == "submissions_closed"


def test_case4_judge_a_sees_own_11_scores(client):
    r = client.get("/api/judge/scores", headers=JUDGE_A)
    assert r.status_code == 200
    body = r.json()
    rows = body["scores"] if isinstance(body, dict) else body
    expected = sum(1 for s in load_fixture_json()["scores"] if s["judge"] == "jdg_24")
    assert expected == 11
    assert len(rows) == 11
    assert {row["judge"] for row in rows} == {"jdg_24"}


def test_case4_body_is_a_json_list(client):
    # Contract wording: "200, JSON list of jdg_24's 11 scores only".
    body = client.get("/api/judge/scores", headers=JUDGE_A).json()
    assert isinstance(body, list)


def test_case5_judge_b_refused_peer_scores(client):
    r = client.get("/api/judges/jdg_24/scores", headers=JUDGE_B)
    assert r.status_code == 403
    assert "jdg_24" not in r.text


def test_case5_peer_url_is_real_for_allowed_callers(client):
    # acceptance.md: peer_scores must really return judge_a's scores to judge_a / organizer.
    for h in (JUDGE_A, ORGANIZER):
        r = client.get("/api/judges/jdg_24/scores", headers=h)
        assert r.status_code == 200
        assert len(r.json()["scores"]) == 11


def test_case6_participant_blocked(client):
    assert client.get("/api/judge/scores", headers=PARTICIPANT).status_code == 403


def test_case7_csv_export(client):
    r = client.get("/api/export.csv", headers=ORGANIZER)
    assert r.status_code == 200
    first = r.text.splitlines()[0]
    assert "," in first
    assert first == "rank,project_id,title,team,track,n_reviews,raw_mean,normalized_z,low_confidence"
