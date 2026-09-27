"""contracts/scoring.md behavior cases 1-7 and listed edge cases, against dogfood.core.scoring.

All expected numbers are hand-computed in the comments (population sd, as the contract states).
"""

import pytest

from conftest import load_fixture_json
from dogfood.core.scoring import Review, effective_reviews, normalize_weights, score

TOL = 1e-9


def by_project(results):
    return {r.project: r for r in results}


# --- case 1 -------------------------------------------------------------------------
def test_case1_constant_judge_contributes_zero():
    # q:1. A: P1=2, P2=4 -> mu 3, sd sqrt(((2-3)^2 + (4-3)^2)/2) = 1 -> P1 z=-1, P2 z=+1.
    # B: P1=5, P2=5 -> sd 0 -> contributes 0.
    # P1 z = (-1 + 0)/2 = -0.5 ; P2 z = (+1 + 0)/2 = +0.5
    # raw P1 = (2+5)/2 = 3.5 ; raw P2 = (4+5)/2 = 4.5 ; rank P2=1, P1=2
    reviews = [Review("A", "P1", {"q": 2}), Review("A", "P2", {"q": 4}),
               Review("B", "P1", {"q": 5}), Review("B", "P2", {"q": 5})]
    res = by_project(score({"q": 1}, reviews, ["P1", "P2"]))
    assert res["P1"].z == pytest.approx(-0.5, abs=TOL)
    assert res["P2"].z == pytest.approx(0.5, abs=TOL)
    assert res["P1"].raw_mean == pytest.approx(3.5, abs=TOL)
    assert res["P2"].raw_mean == pytest.approx(4.5, abs=TOL)
    assert res["P2"].rank == 1
    assert res["P1"].rank == 2
    assert res["P1"].n_reviews == 2 and res["P2"].n_reviews == 2
    # Method step 8: each project has 1 review from an informative judge (A) and 1 from B,
    # who is informative on no criterion -> 1 < 2 -> low_confidence true for both.
    assert res["P1"].low_confidence is True
    assert res["P2"].low_confidence is True


# --- case 2 -------------------------------------------------------------------------
def test_case2_offsets_removed():
    # A: P1=1, P2=3 -> mu 2, sd 1 -> -1, +1.   B: P1=3, P2=5 -> mu 4, sd 1 -> -1, +1.
    # P1 z = (-1 + -1)/2 = -1 ; P2 z = +1
    reviews = [Review("A", "P1", {"q": 1}), Review("A", "P2", {"q": 3}),
               Review("B", "P1", {"q": 3}), Review("B", "P2", {"q": 5})]
    res = by_project(score({"q": 1}, reviews, ["P1", "P2"]))
    assert res["P1"].z == pytest.approx(-1.0, abs=TOL)
    assert res["P2"].z == pytest.approx(1.0, abs=TOL)
    assert res["P2"].rank == 1 and res["P1"].rank == 2
    # Both reviews of each project come from informative judges -> 2 -> not low confidence.
    assert res["P1"].low_confidence is False and res["P2"].low_confidence is False


# --- case 3 -------------------------------------------------------------------------
def test_case3_single_review_judge():
    # A has one review -> not informative (needs >= 2 reviews) -> z 0. raw = 4.
    res = by_project(score({"q": 1}, [Review("A", "P1", {"q": 4})], ["P1"]))
    assert res["P1"].z == pytest.approx(0.0, abs=TOL)
    assert res["P1"].raw_mean == pytest.approx(4.0, abs=TOL)
    assert res["P1"].low_confidence is True
    assert res["P1"].n_reviews == 1


# --- case 4 -------------------------------------------------------------------------
def test_case4_weights_applied_to_raw_and_z():
    # weights f:3, q:1 -> w_f = 3/4 = 0.75, w_q = 1/4 = 0.25
    # raw P1 = 0.75*5 + 0.25*1 = 4.0 ; raw P2 = 0.75*1 + 0.25*5 = 2.0
    # A on f: 5,1 -> mu 3, sd 2 -> P1 +1, P2 -1.  A on q: 1,5 -> mu 3, sd 2 -> P1 -1, P2 +1
    # z P1 = 0.75*(+1) + 0.25*(-1) = 0.5 ; z P2 = 0.75*(-1) + 0.25*(+1) = -0.5
    reviews = [Review("A", "P1", {"f": 5, "q": 1}), Review("A", "P2", {"f": 1, "q": 5})]
    res = by_project(score({"f": 3, "q": 1}, reviews, ["P1", "P2"]))
    assert res["P1"].raw_mean == pytest.approx(4.0, abs=TOL)
    assert res["P2"].raw_mean == pytest.approx(2.0, abs=TOL)
    assert res["P1"].z == pytest.approx(0.5, abs=TOL)
    assert res["P2"].z == pytest.approx(-0.5, abs=TOL)
    assert res["P1"].rank == 1 and res["P2"].rank == 2


def test_case4_normalized_weights():
    w = normalize_weights({"f": 3, "q": 1})
    assert w["f"] == pytest.approx(0.75, abs=TOL)
    assert w["q"] == pytest.approx(0.25, abs=TOL)


# --- case 5 -------------------------------------------------------------------------
def test_case5_zero_review_project_ranked_last():
    reviews = [Review("A", "P1", {"q": 2}), Review("A", "P2", {"q": 4}),
               Review("B", "P1", {"q": 5}), Review("B", "P2", {"q": 5})]
    res = by_project(score({"q": 1}, reviews, ["P1", "P2", "P3"]))
    p3 = res["P3"]
    assert p3.n_reviews == 0
    assert p3.raw_mean is None
    assert p3.z is None
    assert p3.rank == 3
    assert p3.low_confidence is True
    # case 1 values unchanged by the extra empty project
    assert res["P2"].rank == 1 and res["P1"].rank == 2


def test_case5_zero_review_projects_ordered_by_id_after_reviewed():
    # Method step 9: zero-review projects ranked after all reviewed ones, by id.
    reviews = [Review("A", "P5", {"q": 1})]
    res = by_project(score({"q": 1}, reviews, ["P9", "P5", "P7"]))
    assert res["P5"].rank == 1
    assert res["P7"].rank == 2
    assert res["P9"].rank == 3


# --- case 6 -------------------------------------------------------------------------
def test_case6_identical_z_and_raw_lower_id_first():
    # A gives P1=3, P2=3 -> sd 0 -> z 0 for both ; raw 3 for both -> tie -> P1 before P2.
    reviews = [Review("A", "P2", {"q": 3}), Review("A", "P1", {"q": 3})]
    res = by_project(score({"q": 1}, reviews, ["P2", "P1"]))
    assert res["P1"].rank == 1
    assert res["P2"].rank == 2


def test_case6_equal_z_breaks_on_raw_mean_desc():
    # Method step 7: z desc, then raw_mean desc. All judges constant -> z 0 everywhere.
    # A: P1=2, P2=2 (sd 0). B: P2=4 only... B has 1 review -> uninformative.
    # z P1 = 0, z P2 = 0 ; raw P1 = 2, raw P2 = (2+4)/2 = 3 -> P2 ranks first.
    reviews = [Review("A", "P1", {"q": 2}), Review("A", "P2", {"q": 2}), Review("B", "P2", {"q": 4})]
    res = by_project(score({"q": 1}, reviews, ["P1", "P2"]))
    assert res["P2"].rank == 1 and res["P1"].rank == 2


# --- case 7 -------------------------------------------------------------------------
@pytest.mark.parametrize("criteria", [{}, {"q": 0}, {"q": -1}, {"f": 1, "q": 0}, {"f": 1, "q": -0.5}])
def test_case7_invalid_weights_raise(criteria):
    reviews = [Review("A", "P1", {"q": 3, "f": 3})]
    with pytest.raises(ValueError):
        score(criteria, reviews, ["P1"])


# --- edge cases ---------------------------------------------------------------------
def test_edge_informative_on_one_criterion_constant_on_other():
    # f, q equal weight 0.5 each. A: P1 f=2 q=3 ; P2 f=4 q=3.
    # f: mu 3 sd 1 -> P1 -1, P2 +1.  q: sd 0 -> 0.
    # z P1 = 0.5*(-1) = -0.5 ; z P2 = +0.5 ; raw P1 = 2.5, raw P2 = 3.5
    reviews = [Review("A", "P1", {"f": 2, "q": 3}), Review("A", "P2", {"f": 4, "q": 3})]
    res = by_project(score({"f": 1, "q": 1}, reviews, ["P1", "P2"]))
    assert res["P1"].z == pytest.approx(-0.5, abs=TOL)
    assert res["P2"].z == pytest.approx(0.5, abs=TOL)
    assert res["P1"].raw_mean == pytest.approx(2.5, abs=TOL)
    assert res["P2"].raw_mean == pytest.approx(3.5, abs=TOL)


def test_edge_review_for_project_not_in_projects_is_ignored():
    # Same as case 2 plus a review by A on P9 (not canonical). If it were used, A's stats
    # would change (values 1,3,5 -> mu 3). Ignored -> identical to case 2.
    reviews = [Review("A", "P1", {"q": 1}), Review("A", "P2", {"q": 3}), Review("A", "P9", {"q": 5}),
               Review("B", "P1", {"q": 3}), Review("B", "P2", {"q": 5})]
    res = by_project(score({"q": 1}, reviews, ["P1", "P2"]))
    assert set(res) == {"P1", "P2"}
    assert res["P1"].z == pytest.approx(-1.0, abs=TOL)
    assert res["P2"].z == pytest.approx(1.0, abs=TOL)


def test_edge_criteria_not_in_config_are_ignored():
    # 'x' is not configured; must not affect raw or z. Result equals case 2.
    reviews = [Review("A", "P1", {"q": 1, "x": 5}), Review("A", "P2", {"q": 3, "x": 1}),
               Review("B", "P1", {"q": 3, "x": 5}), Review("B", "P2", {"q": 5, "x": 1})]
    res = by_project(score({"q": 1}, reviews, ["P1", "P2"]))
    assert res["P1"].raw_mean == pytest.approx(2.0, abs=TOL)   # (1+3)/2
    assert res["P2"].raw_mean == pytest.approx(4.0, abs=TOL)   # (3+5)/2
    assert res["P1"].z == pytest.approx(-1.0, abs=TOL)


def test_ranks_are_1_to_n_without_ties():
    reviews = [Review("A", p, {"q": 3}) for p in ("P1", "P2", "P3", "P4")]
    res = score({"q": 1}, reviews, ["P4", "P3", "P2", "P1"])
    assert sorted(r.rank for r in res) == [1, 2, 3, 4]


# --- duplicate merge (fixtures-import.md case 7, computed by core/scoring.effective_reviews) ---
def test_duplicate_merge_on_fixture_reviews():
    data = load_fixture_json()
    reviews = [Review(s["judge"], s["project"], s["criteria"]) for s in data["scores"]]
    merged = [r for r in effective_reviews(reviews, {"prj_07": "prj_41"}) if r.project == "prj_41"]
    # Contract case 7: merged set = 6 reviewers.
    assert sorted(r.judge for r in merged) == sorted(["jdg_19", "jdg_21", "jdg_26", "jdg_01", "jdg_12", "jdg_18"])
    assert all(r.project != "prj_07" for r in effective_reviews(reviews, {"prj_07": "prj_41"}))
    # Judges who scored both keep their canonical (prj_41) score, read straight from the file.
    canon = {s["judge"]: s["criteria"] for s in data["scores"] if s["project"] == "prj_41"}
    only07 = {s["judge"]: s["criteria"] for s in data["scores"] if s["project"] == "prj_07"}
    got = {r.judge: dict(r.criteria) for r in merged}
    for j in ("jdg_19", "jdg_21", "jdg_26"):
        assert got[j] == canon[j]
    for j in ("jdg_01", "jdg_12"):
        assert got[j] == only07[j]
    # Nothing else in the file changes.
    others = [r for r in effective_reviews(reviews, {"prj_07": "prj_41"}) if r.project not in ("prj_41",)]
    assert len(others) == sum(1 for s in data["scores"] if s["project"] not in ("prj_07", "prj_41"))
