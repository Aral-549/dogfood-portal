"""Golden cases for contracts/confidence.md (prize-line confidence + tie-breaker assignment).

Third verification pass, 2026-09-27. Every expected value comes from the contract tables or
from hand arithmetic written in the comment next to it (draws enumerated by hand). Nothing is
taken from running dogfood.core.confidence. Case 7 is an invariant check on the real fixtures.
No mocks or monkeypatching: core tests call the pure functions with hand-built inputs; HTTP
tests boot the real app through conftest.portal().
A failing test here means the implementation disagrees with the contract: keep it failing.
"""

import csv
import io

import pytest

from conftest import JUDGE_A, ORGANIZER, PARTICIPANT, load_fixture_json, portal
from dogfood import services as svc
from dogfood.core.confidence import prize_confidence, tiebreak_assign
from dogfood.core.scoring import ProjectResult, ScoredReview

JSON = {"Accept": "application/json"}
CONF_HEADER = "rank,project_id,title,p_top_k,close_call,unreviewed"          # confidence.md "CSV" (amended)
RESULTS_HEADER = "rank,project_id,title,team,track,n_reviews,raw_mean,normalized_z,low_confidence"  # csv-export.md


def reviews(**zs):
    """ScoredReview per z_r; judge ids are distinct per review (irrelevant to confidence)."""
    out = []
    for project, values in zs.items():
        for i, z in enumerate(values):
            out.append(ScoredReview(judge=f"j{i}", project=project, r=3.0, z=z))
    return out


def ranking(*order):
    """Published point-estimate ranks in the given order (rank 1 first)."""
    return [ProjectResult(project=p, n_reviews=1, raw_mean=3.0, z=0.0, rank=i, raw_rank=i, low_confidence=False)
            for i, p in enumerate(order, 1)]


def p3(report, project):
    return round(report.projects[project].p_top_k, 3)


# --- behavior cases 1-5 (exact mode) --------------------------------------------------------

def test_case1_certain_winner():
    # A {+1,+1}: all 4 draws have mean +1. B {0,0}: all 4 draws mean 0. A > B always, k=1.
    # Space = 2^2 * 2^2 = 16 <= 20000 -> exact.
    rep = prize_confidence(reviews(A=[1.0, 1.0], B=[0.0, 0.0]), ranking("A", "B"), k=1)
    assert rep.method == "exact"
    assert p3(rep, "A") == 1.000 and p3(rep, "B") == 0.000
    assert not rep.projects["A"].close_call and not rep.projects["B"].close_call
    assert not rep.projects["A"].unreviewed and not rep.projects["B"].unreviewed


def test_case2_tie_break_by_published_rank_favours_A():
    # A {+1,-1}, published rank 1; B {0}, rank 2; k=1. Space = 2^2 * 1^1 = 4.
    # A draws: (+1,+1)->+1, (+1,-1)->0, (-1,+1)->0, (-1,-1)->-1. B always 0.
    # A wins at +1 (1 draw) and at 0 by the published-rank tie-break (2 draws): 3/4 = 0.750.
    # B wins only when A = -1: 1/4 = 0.250. Both in [0.2, 0.8] -> close calls.
    rep = prize_confidence(reviews(A=[1.0, -1.0], B=[0.0]), ranking("A", "B"), k=1)
    assert rep.method == "exact"
    assert p3(rep, "A") == 0.750 and p3(rep, "B") == 0.250
    assert rep.projects["A"].close_call and rep.projects["B"].close_call


def test_case3_mirror_tie_break_favours_B():
    # Same draws as case 2, but B has published rank 1: A wins only at +1 (1 of 4) = 0.250;
    # B wins at 0 (2 draws, tie-break) and at -1 (1 draw) = 3/4 = 0.750. Both close calls.
    rep = prize_confidence(reviews(A=[1.0, -1.0], B=[0.0]), ranking("B", "A"), k=1)
    assert rep.method == "exact"
    assert p3(rep, "A") == 0.250 and p3(rep, "B") == 0.750
    assert rep.projects["A"].close_call and rep.projects["B"].close_call


def test_case4_single_reviews_never_move_and_unreviewed():
    # One review each: every resample equals the original. Order +1 > 0 > -1; k=2 -> A, B in.
    rep = prize_confidence(reviews(A=[1.0], B=[0.0], C=[-1.0]), ranking("A", "B", "C", "D"), k=2)
    assert rep.method == "exact"
    assert (p3(rep, "A"), p3(rep, "B"), p3(rep, "C"), p3(rep, "D")) == (1.000, 1.000, 0.000, 0.000)
    assert rep.projects["D"].unreviewed and not rep.projects["D"].close_call
    assert not any(rep.projects[p].unreviewed for p in "ABC")
    assert not any(rep.projects[p].close_call for p in "ABCD")


def test_case5_k_at_least_reviewed_count():
    # k=5 and only three reviewed projects: every replicate puts all three in the top 5.
    rep = prize_confidence(reviews(A=[1.0, -1.0], B=[0.5, 0.0], C=[-2.0, 2.0]), ranking("A", "B", "C"), k=5)
    assert {p: p3(rep, p) for p in "ABC"} == {"A": 1.000, "B": 1.000, "C": 1.000}
    assert not any(rep.projects[p].close_call for p in "ABC")


def test_edge_unreviewed_never_in_top_k_even_when_k_exceeds_projects():
    # "Unreviewed projects are never in the top k": k=3, only A reviewed.
    rep = prize_confidence(reviews(A=[0.0]), ranking("A", "B", "C"), k=3)
    assert p3(rep, "A") == 1.000
    for p in "BC":
        assert rep.projects[p].p_top_k == 0 and rep.projects[p].unreviewed and not rep.projects[p].close_call


# --- case 6: invalid k -------------------------------------------------------------------------

@pytest.mark.parametrize("k", [0, -1, 1.5, "3", None, float("inf"), float("nan")])
def test_case6_invalid_k_raises_value_error(k):
    with pytest.raises(ValueError):
        prize_confidence(reviews(A=[1.0]), ranking("A"), k=k)


# --- method selection (contract Method step 4) --------------------------------------------------

def test_method_exact_at_16384_distinct_replicates():
    # 7 projects x 2 reviews: 4^7 = 16384 <= 20000 -> exact.
    zs = {f"P{i}": [float(i), float(-i)] for i in range(7)}
    rep = prize_confidence(reviews(**zs), ranking(*zs), k=3)
    assert rep.method == "exact"


def test_method_monte_carlo_above_20000_distinct_replicates():
    # 8 projects x 2 reviews: 4^8 = 65536 > 20000 -> Monte Carlo with `replicates` draws.
    zs = {f"P{i}": [float(i), float(-i)] for i in range(8)}
    rep = prize_confidence(reviews(**zs), ranking(*zs), k=3, seed=0, replicates=2000)
    assert rep.method == "monte_carlo"
    assert rep.replicates == 2000


def test_monte_carlo_same_seed_same_output_and_k_winners_per_replicate():
    zs = {f"P{i}": [float(i % 3), float(-(i % 4)), 0.5] for i in range(8)}   # 27^8 > 20000
    one = prize_confidence(reviews(**zs), ranking(*zs), k=2, seed=7, replicates=500)
    two = prize_confidence(reviews(**zs), ranking(*zs), k=2, seed=7, replicates=500)
    assert one == two
    wins = [one.projects[p].p_top_k * 500 for p in zs]
    assert all(abs(w - round(w)) < 1e-9 for w in wins)
    assert sum(round(w) for w in wins) == 2 * 500


# --- edge cases -----------------------------------------------------------------------------------

def test_edge_all_uninformative_decided_by_tie_breaks():
    # All z_r = 0 (uninformative judges). Every draw ties at 0; published rank decides: A (rank 1).
    rep = prize_confidence(reviews(A=[0.0, 0.0], B=[0.0, 0.0], C=[0.0]), ranking("A", "B", "C"), k=1)
    assert (p3(rep, "A"), p3(rep, "B"), p3(rep, "C")) == (1.000, 0.000, 0.000)


def test_edge_float_noise_rounded_to_12_dp():
    # A {0.1, 0.2} published rank 2; B {0.15} rank 1; k=1. Space 4.
    # Mixed draws: (0.1 + 0.2) / 2 = 0.15000000000000002 in floats, which must TIE with 0.15 after
    # rounding to 12 dp, so B wins those by published rank. A wins only at (0.2, 0.2): 1/4.
    rep = prize_confidence(reviews(A=[0.1, 0.2], B=[0.15]), ranking("B", "A"), k=1)
    assert p3(rep, "A") == 0.250 and p3(rep, "B") == 0.750


def test_edge_two_resampled_projects():
    # A {+1,-1} rank 1 vs B {+1,-1} rank 2, k=1. Space 4*4 = 16. A wins when meanA >= meanB (tie -> A):
    # meanA in {+1 (1/4), 0 (2/4), -1 (1/4)}, same for B.
    # P(A >= B) = P(A=+1) + P(A=0)*P(B<=0) + P(A=-1)*P(B=-1) = 1/4 + 1/2*3/4 + 1/4*1/4
    #           = 4/16 + 6/16 + 1/16 = 11/16 = 0.6875. B = 5/16 = 0.3125. Both close calls.
    rep = prize_confidence(reviews(A=[1.0, -1.0], B=[1.0, -1.0]), ranking("A", "B"), k=1)
    assert rep.projects["A"].p_top_k == pytest.approx(11 / 16)
    assert rep.projects["B"].p_top_k == pytest.approx(5 / 16)
    assert rep.projects["A"].close_call and rep.projects["B"].close_call


def test_edge_projects_not_in_ranking_are_ignored():
    # Reviews of a project absent from the published ranking (e.g. a superseded duplicate)
    # must not appear in the report.
    rep = prize_confidence(reviews(A=[1.0], X=[5.0]), ranking("A"), k=1)
    assert set(rep.projects) == {"A"}
    assert p3(rep, "A") == 1.000


# --- tie-breaker assignment, cases 8-10 ------------------------------------------------------------

def test_case8_comparability_beats_load_then_already_assigned():
    # P (p=0.5) first, |0.5-0.5| = 0 < |0.3-0.5| = 0.2. J1 reviewed Q (another close call) -> (a)
    # wins P despite load 1. Q: J1 already assigned -> J2. J0 covers only track u: never a candidate.
    new, stuck = tiebreak_assign(
        close_calls=[("Q", 0.3), ("P", 0.5)],
        project_tracks={"P": "t", "Q": "t"},
        judge_tracks={"J0": {"u"}, "J1": {"t"}, "J2": {"t"}},
        assignments=[("J1", "Q")],
        reviewed_by={"J1": {"Q"}},
        conflicts=set(),
        already_tiebroken=set(),
    )
    assert new == [("J1", "P"), ("J2", "Q")]
    assert stuck == []


def test_case9_no_candidate_reported():
    # Only J1 and J2 cover t: J1 already assigned to P, J2 conflicted with P.
    new, stuck = tiebreak_assign([("P", 0.5)], {"P": "t"}, {"J1": {"t"}, "J2": {"t"}, "J3": {"u"}},
                                 [("J1", "P")], {"J1": {"P"}}, {("J2", "P")}, set())
    assert new == [] and stuck == ["P"]


def test_case10_second_run_adds_nothing_for_tiebroken_projects():
    args = dict(close_calls=[("P", 0.5), ("Q", 0.4)], project_tracks={"P": "t", "Q": "t"},
                judge_tracks={"J1": {"t"}, "J2": {"t"}, "J3": {"t"}}, reviewed_by={}, conflicts=set())
    first, stuck = tiebreak_assign(assignments=[], already_tiebroken=set(), **args)
    # P first (distance 0), J1 by id (all load 0); Q: J1 load 1 now, J2 load 0 -> J2.
    assert first == [("J1", "P"), ("J2", "Q")] and stuck == []
    second, stuck2 = tiebreak_assign(assignments=first, already_tiebroken={p for _, p in first}, **args)
    assert second == [] and stuck2 == []


def test_tiebreak_order_ties_on_distance_broken_by_project_id():
    # |0.25-0.5| = |0.75-0.5| = 0.25 exactly: project id order, A before B.
    # A takes J1 (id), then B takes J2 (J1 now has load 1).
    new, _ = tiebreak_assign([("B", 0.25), ("A", 0.75)], {"A": "t", "B": "t"}, {"J1": {"t"}, "J2": {"t"}},
                             [], {}, set(), set())
    assert new == [("J1", "A"), ("J2", "B")]


def test_tiebreak_load_breaks_ties_before_id():
    # (a) equal (0 reviews on close calls), (b) J1 has load 2, J2 load 0 -> J2.
    new, _ = tiebreak_assign([("P", 0.5)], {"P": "t"}, {"J1": {"t"}, "J2": {"t"}},
                             [("J1", "X"), ("J1", "Y")], {"J1": {"X", "Y"}}, set(), set())
    assert new == [("J2", "P")]


def test_tiebreak_reviews_on_non_close_projects_do_not_count_for_comparability():
    # J1 reviewed X (NOT a close call) and is assigned there (load 1): (a) is 0 for both, (b) J2 wins.
    new, _ = tiebreak_assign([("P", 0.5)], {"P": "t"}, {"J1": {"t"}, "J2": {"t"}},
                             [("J1", "X")], {"J1": {"X"}}, set(), set())
    assert new == [("J2", "P")]


def test_tiebreak_no_judge_covers_track_uses_all_judges():
    new, stuck = tiebreak_assign([("P", 0.5)], {"P": "t"}, {"J1": {"u"}, "J2": {"v"}}, [], {}, set(), set())
    assert new == [("J1", "P")] and stuck == []


def test_tiebreak_project_without_track_uses_all_judges():
    new, _ = tiebreak_assign([("P", 0.5)], {"P": None}, {"J2": {"u"}, "J1": set()}, [], {}, set(), set())
    assert new == [("J1", "P")]


def test_tiebreak_never_picks_conflicted_judge_even_if_preferred():
    # J1 would win on (a) (reviewed close call Q) but is conflicted with P.
    new, _ = tiebreak_assign([("P", 0.5), ("Q", 0.3)], {"P": "t", "Q": "t"}, {"J1": {"t"}, "J2": {"t"}},
                             [("J1", "Q")], {"J1": {"Q"}}, {("J1", "P")}, set())
    assert ("J1", "P") not in new
    assert new[0] == ("J2", "P")


# --- case 7: fixture invariant, and the HTTP surface ---------------------------------------------

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with portal(tmp_path_factory.mktemp("confidence")) as c:
        yield c


def test_case7_fixture_monte_carlo_deterministic_k_winners(client):
    conn = client.app.state.conn
    one = svc.analysis(conn, "evt_01", 3)["confidence"]
    two = svc.analysis(conn, "evt_01", 3)["confidence"]
    assert one.method == "monte_carlo"
    assert one == two
    # Each replicate has exactly 3 winners: sum of wins over projects == 3 * replicates.
    wins = [c.p_top_k * one.replicates for c in one.projects.values()]
    assert all(abs(w - round(w)) < 1e-6 for w in wins)
    assert sum(round(w) for w in wins) == 3 * one.replicates
    assert round(sum(c.p_top_k for c in one.projects.values()), 3) == 3.000


def test_edge_fixture_merged_duplicate_uses_effective_reviews(client):
    # fixtures-import.md case 7: prj_41 has 6 effective reviews; prj_07 is superseded and absent.
    a = svc.analysis(client.app.state.conn, "evt_01", 3)
    assert "prj_07" not in a["confidence"].projects
    assert "prj_41" in a["confidence"].projects
    assert {r.project: r.n_reviews for r in a["results"]}["prj_41"] == 6
    assert set(a["confidence"].projects) == {r.project for r in a["results"]}
    assert len(a["confidence"].projects) == 40


@pytest.mark.parametrize("headers,expected", [(JUDGE_A, 403), (PARTICIPANT, 403), ({}, 401)],
                         ids=["judge", "participant", "visitor"])
def test_http_confidence_csv_is_organizer_only(client, headers, expected):
    r = client.get("/api/confidence.csv", headers=headers)
    assert r.status_code == expected
    assert "p_top_k" not in r.text


def test_http_confidence_csv_header_and_rows(client):
    r = client.get("/api/confidence.csv", headers=ORGANIZER)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    text = r.content.decode("utf-8")
    assert text.split("\r\n")[0] == CONF_HEADER
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 40
    assert [int(x["rank"]) for x in rows] == list(range(1, 41))
    assert "prj_07" not in {x["project_id"] for x in rows}
    for x in rows:
        p = x["p_top_k"]
        assert len(p.split(".")[1]) == 3, p                      # reported to 3 dp
        assert 0.0 <= float(p) <= 1.0
        assert x["close_call"] in ("true", "false") and x["unreviewed"] in ("true", "false")
        assert x["unreviewed"] == "false"                        # every fixture project has reviews


def test_http_results_csv_header_unchanged(client):
    r = client.get("/api/export.csv", headers=ORGANIZER)
    assert r.status_code == 200
    assert r.content.decode("utf-8").split("\r\n")[0] == RESULTS_HEADER


@pytest.mark.parametrize("k", ["0", "-1", "abc", "1e999", "99999999999999999999", "3.5", "nan", "", "%00", "1"])
def test_http_odd_k_never_500(client, k):
    for path in ("/organizer", "/api/confidence.csv"):
        r = client.get(path, params={"k": k}, headers=ORGANIZER)
        assert r.status_code == 200, (path, k)


def test_http_dashboard_close_call_flags_match_csv(client):
    csv_rows = list(csv.DictReader(io.StringIO(client.get("/api/confidence.csv?k=3", headers=ORGANIZER).text)))
    page = client.get("/organizer?k=3", headers=ORGANIZER)
    assert page.status_code == 200
    assert "P(top 3)" in page.text
    assert page.text.count(">close call<") == sum(x["close_call"] == "true" for x in csv_rows)


def test_http_judge_and_participant_pages_never_show_p_top_k(client):
    for headers, path in ((JUDGE_A, "/judge"), (PARTICIPANT, "/me"), (JUDGE_A, "/organizer"),
                          (PARTICIPANT, "/organizer")):
        r = client.get(path, headers=headers)
        assert "P(top" not in r.text and "p_top_k" not in r.text, path


def test_http_organizer_page_renders_under_two_seconds(client):
    import time
    t0 = time.perf_counter()
    r = client.get("/organizer", headers=ORGANIZER)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 2.0, elapsed


# --- tie-breaker over HTTP -------------------------------------------------------------------------

def _audit(conn, action):
    import json
    return [json.loads(r["detail"]) | {"subject": r["subject"]} for r in
            conn.execute("SELECT subject, detail FROM audit_log WHERE action = ? ORDER BY id", (action,))]


@pytest.mark.parametrize("headers,expected", [(JUDGE_A, 403), (PARTICIPANT, 403), ({}, 401)],
                         ids=["judge", "participant", "visitor"])
def test_http_tiebreak_is_organizer_only(tmp_path, headers, expected):
    with portal(tmp_path) as c:
        r = c.post("/organizer/tiebreak", json={"k": 3}, headers={**headers, **JSON})
        assert r.status_code == expected
        assert _audit(c.app.state.conn, "assignment.tiebreak") == []


def test_http_tiebreak_audited_invariants_and_idempotent(tmp_path):
    with portal(tmp_path) as c:
        conn = c.app.state.conn
        before = {(r[0], r[1]) for r in conn.execute("SELECT judge_id, project_id FROM assignments")}
        close = {x["project_id"] for x in csv.DictReader(io.StringIO(
            c.get("/api/confidence.csv?k=3", headers=ORGANIZER).text)) if x["close_call"] == "true"}
        assert close, "fixture has close calls at k=3 (otherwise this test is vacuous)"
        r = c.post("/organizer/tiebreak", json={"k": 3}, headers=ORGANIZER)
        assert r.status_code == 303
        rows = _audit(conn, "assignment.tiebreak")
        assert len(rows) == 1
        added = [tuple(x) for x in rows[0]["added"]]
        stuck = rows[0]["no_candidate"]
        assert added
        assert {p for _, p in added} | set(stuck) == close          # every close call handled once
        assert len({p for _, p in added}) == len(added)             # ONE judge per project
        for j, p in added:
            assert (j, p) not in before                             # not already assigned
        after = {(r[0], r[1]) for r in conn.execute("SELECT judge_id, project_id FROM assignments")}
        assert after == before | set(added)
        # Case 10: a second run with no new scores adds nothing for those projects.
        assert c.post("/organizer/tiebreak", json={"k": 3}, headers=ORGANIZER).status_code == 303
        rows = _audit(conn, "assignment.tiebreak")
        assert len(rows) == 2
        assert all(p not in {pp for _, pp in added} for _, p in rows[1]["added"])


@pytest.mark.parametrize("body", [{"k": "abc"}, {"k": 1e308}, {"k": [3]}, {"k": {"a": 1}}, {"k": -5}, {"k": 0},
                                  {"k": 10 ** 30}, {"k": None}, {"k": "1e999"}, {}])
def test_http_tiebreak_odd_k_never_500(tmp_path_factory, body):
    with portal(tmp_path_factory.mktemp("tb")) as c:
        r = c.post("/organizer/tiebreak", json=body, headers=ORGANIZER)
        assert r.status_code == 303


def test_http_tiebreak_infinite_k_raw_json_never_500(client):
    r = client.post("/organizer/tiebreak", content=b'{"k": 1e999}',
                    headers={**ORGANIZER, "Content-Type": "application/json"})
    assert r.status_code < 500


def test_http_confidence_csv_title_formula_injection_defended(tmp_path):
    # csv-export.md case 5 applies to every organizer CSV cell holding user text.
    with portal(tmp_path) as c:
        assert c.post("/organizer/event", json={"submissions_close": "2099-01-01T00:00:00Z"},
                      headers=ORGANIZER).status_code == 303
        titles = ["=HYPERLINK(\"http://x\")", "+1+1", "-2", "@SUM(A1)"]
        for t in titles:
            r = c.post("/api/projects", json={"title": t, "summary": "s"}, headers=PARTICIPANT)
            assert r.status_code == 201, r.text
        text = c.get("/api/confidence.csv", headers=ORGANIZER).text
        rows = list(csv.DictReader(io.StringIO(text)))
        got = {x["title"] for x in rows}
        for t in titles:
            assert "'" + t in got, t
            assert t not in got, t
        new = [x for x in rows if x["title"].startswith("'")]
        assert all(x["unreviewed"] == "true" and x["p_top_k"] == "0.000" and x["close_call"] == "false" for x in new)


def test_http_tiebreak_never_picks_excluded_or_conflicted_judge(tmp_path):
    # Hand setup from fixtures.json: prj_37 is track trk_07 (team tm_37, 3 members). trk_07 is
    # covered by jdg_04, jdg_11, jdg_12, jdg_14, jdg_22, jdg_24; jdg_04, jdg_11, jdg_22, jdg_24
    # reviewed (so are assigned to) prj_37. Unassigned candidates: jdg_12 and jdg_14.
    # We add N (new judge, trk_07, no reviews, load 0: would win on load) and EXCLUDE it, and make
    # jdg_12 conflicted by joining tm_37. Neither changes any counted review, so the ranking and
    # the close calls stay as they were. The only valid pick for prj_37 is then jdg_14.
    fx = load_fixture_json()
    prj37 = next(p for p in fx["projects"] if p["id"] == "prj_37")
    assert prj37["track"] == "trk_07" and prj37["team"] == "tm_37"
    assert {j["id"] for j in fx["judges"] if "trk_07" in j["tracks"]} == \
           {"jdg_04", "jdg_11", "jdg_12", "jdg_14", "jdg_22", "jdg_24"}
    assert {s["judge"] for s in fx["scores"] if s["project"] == "prj_37"} == {"jdg_04", "jdg_11", "jdg_22", "jdg_24"}
    with portal(tmp_path) as c:
        conn = c.app.state.conn
        results0 = c.get("/api/export.csv", headers=ORGANIZER).content
        r = c.post("/organizer/judges", json={"email": "tiebreak.new@example.org", "tracks": ["trk_07"]},
                   headers=ORGANIZER)
        assert r.status_code == 303
        n_id = conn.execute("SELECT j.id FROM judges j JOIN users u ON u.id = j.user_id "
                            "WHERE u.email = 'tiebreak.new@example.org'").fetchone()[0]
        r = c.post(f"/organizer/judges/{n_id}/exclude", json={"reason": "not briefed"}, headers=ORGANIZER)
        assert r.status_code == 303
        # jdg_12 (no password yet) gets a set-password link from the organizer, logs in, joins tm_37.
        assert c.post("/organizer/event", json={"submissions_close": "2099-01-01T00:00:00Z"},
                      headers=ORGANIZER).status_code == 303
        email12 = next(j["email"] for j in fx["judges"] if j["id"] == "jdg_12")
        r = c.post("/organizer/judges", json={"email": email12}, headers=ORGANIZER)
        link = r.headers["location"].split("link=")[1]
        assert c.post(link, data={"password": "tiebreak-pass-12"}).status_code == 303
        r = c.post("/login", data={"email": email12, "password": "tiebreak-pass-12"})
        assert r.status_code == 303
        cookie = {"Cookie": f"session={r.cookies.get('session')}"}
        c.cookies.clear()
        code = conn.execute("SELECT invite_code FROM teams WHERE id = 'tm_37'").fetchone()[0]
        assert c.post(f"/join/{code}", headers=cookie).status_code == 303
        # Precondition: nothing counted changed.
        assert c.get("/api/export.csv", headers=ORGANIZER).content == results0
        close = {x["project_id"] for x in csv.DictReader(io.StringIO(
            c.get("/api/confidence.csv?k=3", headers=ORGANIZER).text)) if x["close_call"] == "true"}
        assert "prj_37" in close, close
        assert c.post("/organizer/tiebreak", json={"k": 3}, headers=ORGANIZER).status_code == 303
        added = [tuple(x) for x in _audit(conn, "assignment.tiebreak")[-1]["added"]]
        assert all(j != n_id for j, _ in added)
        assert ("jdg_12", "prj_37") not in added
        assert ("jdg_14", "prj_37") in added
        conflicts = {(r[0], r[1]) for r in conn.execute(
            "SELECT j.id, p.id FROM judges j JOIN team_members m ON m.user_id = j.user_id "
            "JOIN projects p ON p.team_id = m.team_id")}
        assert not set(added) & conflicts


def test_http_close_call_flag_consistent_with_reported_p(tmp_path):
    # Outputs: "p_top_k ... reported to 3 dp" and "close_call: 0.2 <= p_top_k <= 0.8". A row must
    # never read "0.200,false" or "0.800,false": the flag has to agree with the number shown.
    # (With 2000 replicates p moves in steps of 0.0005, so 399/2000 = 0.1995 prints as 0.200.)
    # Scenario: jdg_04 excluded (contract case 8 of judge-agreement.md), every k from 1 to 40.
    with portal(tmp_path) as c:
        assert c.post("/organizer/judges/jdg_04/exclude", json={"reason": "outlier"},
                      headers=ORGANIZER).status_code == 303
        bad = []
        for k in range(1, 41):
            for x in csv.DictReader(io.StringIO(c.get(f"/api/confidence.csv?k={k}", headers=ORGANIZER).text)):
                if x["unreviewed"] == "false" and (x["close_call"] == "true") != (0.2 <= float(x["p_top_k"]) <= 0.8):
                    bad.append((k, x["project_id"], x["p_top_k"], x["close_call"]))
        assert bad == []
