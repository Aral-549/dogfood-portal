"""Golden cases for contracts/judge-agreement.md (as amended 2026-09-27: outlier below -0.3,
at least 4 shared projects).

Third verification pass. Expected values come from the contract table or from hand arithmetic
in the comment next to each assertion; nothing is taken from running dogfood.core.agreement.
Fixture facts (who reviewed what) are counted in fixtures.json with the stdlib only.
No mocks or monkeypatching: core tests call the pure function with hand-built ScoredReviews;
HTTP tests boot the real app through conftest.portal().
A failing test here means the implementation disagrees with the contract: keep it failing.
"""

import csv
import io
import json
import re

import pytest

from conftest import JUDGE_A, ORGANIZER, PARTICIPANT, load_fixture_json, portal
from dogfood.core.agreement import judge_agreement
from dogfood.core.scoring import Review, ScoredReview, score_reviews

JSON = {"Accept": "application/json"}


def sr(judge, project, z):
    return ScoredReview(judge=judge, project=project, r=3.0, z=z)


def paired(j_values, k_values, projects=None):
    """J and K both review P1..Pn; K's z_r is J's consensus (the only other review)."""
    projects = projects or [f"P{i}" for i in range(1, len(j_values) + 1)]
    return ([sr("J", p, z) for p, z in zip(projects, j_values)] +
            [sr("K", p, z) for p, z in zip(projects, k_values)])


def row(report, judge):
    return next(a for a in report if a.judge == judge)


# --- behavior cases 1-7 -------------------------------------------------------------------------

def test_case1_perfect_agreement():
    # J (-1, 0, 0, +1); consensus = K = (-0.5, 0, 0, +0.5) = J / 2 -> Pearson +1.
    # mean_abs_dev = (0.5 + 0 + 0 + 0.5) / 4 = 0.250.
    report, flags = judge_agreement(paired([-1.0, 0.0, 0.0, 1.0], [-0.5, 0.0, 0.0, 0.5]), {"J", "K"})
    j = row(report, "J")
    assert (j.shared, j.agreement, j.mean_abs_dev, j.status) == (4, 1.000, 0.250, "ok")
    assert flags == []  # largest gap 0.5 < 2.0


def test_case2_outlier():
    # J (+1, 0, 0, -1); consensus (-1, 0, 0, +1) = -J -> Pearson -1 < -0.3 -> outlier.
    # mean_abs_dev = (2 + 0 + 0 + 2) / 4 = 1.000.
    report, _ = judge_agreement(paired([1.0, 0.0, 0.0, -1.0], [-1.0, 0.0, 0.0, 1.0]), {"J", "K"})
    j = row(report, "J")
    assert (j.shared, j.agreement, j.mean_abs_dev, j.status) == (4, -1.000, 1.000, "outlier")


def test_case3_only_three_shared_is_insufficient():
    # Perfectly anti-correlated but only 3 shared projects: below the 4-project minimum.
    report, _ = judge_agreement(paired([1.0, 0.0, -1.0], [-1.0, 0.0, 1.0]), {"J", "K"})
    j = row(report, "J")
    assert j.shared == 3 and j.agreement is None and j.status == "insufficient"


def test_case3a_weak_disagreement_is_ok_and_reported():
    # J x = (1, -1, 0, 0), consensus y = (0, 1, 3, -3).
    # mean x = 0, so cov = sum x*y = 1*0 + (-1)*1 + 0 + 0 = -1.
    # Sxx = 1 + 1 = 2. mean y = 0.25, Syy = sum y^2 - 4*0.25^2 = (0+1+9+9) - 0.25 = 18.75.
    # r = -1 / sqrt(2 * 18.75) = -1 / sqrt(37.5) = -1 / 6.12372 = -0.16330 -> -0.163 (in (-0.3, 0)).
    # mean_abs_dev = (|1-0| + |-1-1| + |0-3| + |0+3|) / 4 = (1 + 2 + 3 + 3) / 4 = 2.250.
    report, _ = judge_agreement(paired([1.0, -1.0, 0.0, 0.0], [0.0, 1.0, 3.0, -3.0]), {"J", "K"})
    j = row(report, "J")
    assert (j.shared, j.agreement, j.mean_abs_dev, j.status) == (4, -0.163, 2.250, "ok")


def test_case4_favoritism_flag_independent_of_status():
    # J's review of P: +1.5; others -0.5 and -1.1 -> consensus (-0.5 - 1.1) / 2 = -0.8; gap 2.3.
    # J shares only 1 project (insufficient) but the review is still flagged.
    scored = [sr("J", "P", 1.5), sr("K1", "P", -0.5), sr("K2", "P", -1.1)]
    report, flags = judge_agreement(scored, {"J", "K1", "K2"})
    assert row(report, "J").status == "insufficient"
    assert [(f.judge, f.project) for f in flags] == [("J", "P")]
    assert flags[0].gap == pytest.approx(2.3, abs=1e-3)
    # K1: -0.5 - mean(1.5, -1.1) = -0.5 - 0.2 = -0.7; K2: -1.1 - 0.5 = -1.6: neither flagged.


def test_favoritism_boundary_exactly_two_is_flagged():
    # ">= 2.0": J +1 vs consensus -1 is a gap of exactly 2.0.
    _, flags = judge_agreement([sr("J", "P", 1.0), sr("K", "P", -1.0)], {"J", "K"})
    assert [(f.judge, f.project) for f in flags] == [("J", "P")]


def test_favoritism_just_below_two_not_flagged():
    _, flags = judge_agreement([sr("J", "P", 1.0), sr("K", "P", -0.99)], {"J", "K"})
    assert flags == []


def test_favoritism_is_one_sided():
    # J rates P far BELOW the others (gap -3): not favoritism. K is +3 above J: flagged.
    _, flags = judge_agreement([sr("J", "P", -1.5), sr("K", "P", 1.5)], {"J", "K"})
    assert [(f.judge, f.project) for f in flags] == [("K", "P")]


def test_case5_uninformative_judge():
    scored = paired([0.0, 0.0, 0.0, 0.0], [1.0, -1.0, 0.5, -0.5])
    report, _ = judge_agreement(scored, {"K"})  # J not informative
    j = row(report, "J")
    assert j.status == "uninformative" and j.agreement is None


def test_case5_fixture_jdg_07_is_uninformative(tmp_path):
    # fixtures.json: jdg_07 scored 4,4,4 on every criterion of every project.
    fx = load_fixture_json()
    mine = [s["criteria"] for s in fx["scores"] if s["judge"] == "jdg_07"]
    assert mine and all(set(c.values()) == {4} for c in mine)
    with portal(tmp_path) as c:
        r = c.get("/api/judge-agreement", headers=ORGANIZER)
        assert r.status_code == 200
        j = next(x for x in r.json()["judges"] if x["judge"] == "jdg_07")
        assert j["status"] == "uninformative" and j["agreement"] is None


def test_case6_no_other_reviewers():
    report, flags = judge_agreement([sr("J", "P1", 1.0), sr("J", "P2", -1.0), sr("J", "P3", 0.5),
                                     sr("J", "P4", -0.5)], {"J"})
    j = row(report, "J")
    assert j.shared == 0 and j.agreement is None and j.status == "insufficient"
    assert j.mean_abs_dev is None
    assert flags == []


def test_case7_constant_consensus_is_insufficient():
    report, _ = judge_agreement(paired([1.0, 0.0, -1.0, 0.5], [0.5, 0.5, 0.5, 0.5]), {"J", "K"})
    j = row(report, "J")
    assert j.agreement is None and j.status == "insufficient"


def test_case7_constant_judge_is_insufficient():
    # Zero variance on J's side (informative on paper, e.g. from other criteria).
    report, _ = judge_agreement(paired([0.2, 0.2, 0.2, 0.2], [1.0, -1.0, 0.0, 0.5]), {"J", "K"})
    j = row(report, "J")
    assert j.agreement is None and j.status == "insufficient"


def test_leave_one_out_consensus_with_several_others():
    # P1..P4 each reviewed by J, K1, K2. Consensus for J = mean(K1, K2) = (-1, -0.5, 0.5, 1).
    # J = (-2, -1, 1, 2) = 2 * consensus -> r = +1. mad = (1 + 0.5 + 0.5 + 1) / 4 = 0.750.
    ps = ["P1", "P2", "P3", "P4"]
    scored = ([sr("J", p, z) for p, z in zip(ps, [-2.0, -1.0, 1.0, 2.0])] +
              [sr("K1", p, z) for p, z in zip(ps, [-1.5, -0.5, 0.0, 1.0])] +
              [sr("K2", p, z) for p, z in zip(ps, [-0.5, -0.5, 1.0, 1.0])])
    report, _ = judge_agreement(scored, {"J", "K1", "K2"})
    j = row(report, "J")
    assert (j.shared, j.agreement, j.mean_abs_dev, j.status) == (4, 1.000, 0.750, "ok")


def test_edge_harsh_but_consistent_judge_not_flagged():
    # One criterion "c". H scores (1, 2, 3, 2), L scores (3, 4, 5, 4) on P1..P4.
    # Each judge: mean 2 (resp. 4), population sd sqrt((1 + 0 + 1 + 0) / 4) = sqrt(0.5) = 0.7071.
    # z_r = (-1.4142, 0, +1.4142, 0) for BOTH judges -> r = +1, mad 0, no favoritism.
    # Raw scores would give a gap of 2 on every project (L flagged 4 times) and mad 2.
    ps = ["P1", "P2", "P3", "P4"]
    revs = ([Review("H", p, {"c": v}) for p, v in zip(ps, [1, 2, 3, 2])] +
            [Review("L", p, {"c": v}) for p, v in zip(ps, [3, 4, 5, 4])])
    scored, informative = score_reviews({"c": 1}, revs, ps)
    report, flags = judge_agreement(scored, informative)
    for judge in ("H", "L"):
        a = row(report, judge)
        assert (a.agreement, a.mean_abs_dev, a.status) == (1.000, 0.000, "ok")
    assert flags == []


# --- exclusion over HTTP (cases 8-12) -----------------------------------------------------------

def fixture_reviewers():
    """Canonical project -> set of judges, from fixtures.json (prj_07 folds into prj_41)."""
    out: dict[str, set] = {}
    for s in load_fixture_json()["scores"]:
        p = "prj_41" if s["project"] == "prj_07" else s["project"]
        out.setdefault(p, set()).add(s["judge"])
    return out


def results_rows(c):
    r = c.get("/api/export.csv", headers=ORGANIZER)
    assert r.status_code == 200
    return {x["project_id"]: x for x in csv.DictReader(io.StringIO(r.text))}


def conf_rows(c, k=3):
    r = c.get(f"/api/confidence.csv?k={k}", headers=ORGANIZER)
    assert r.status_code == 200
    return {x["project_id"]: x for x in csv.DictReader(io.StringIO(r.text))}


def audit_rows(c, action):
    return [(r["subject"], json.loads(r["detail"])) for r in c.app.state.conn.execute(
        "SELECT subject, detail FROM audit_log WHERE action = ? ORDER BY id", (action,))]


def exclude(c, judge, reason="scores disagree with every other judge", **kw):
    return c.post(f"/organizer/judges/{judge}/exclude", json={"reason": reason}, headers=ORGANIZER, **kw)


def test_case8_exclusion_leaves_reviews_out_and_is_audited(tmp_path):
    # jdg_04 reviewed prj_04, prj_14, prj_18, prj_37 (fixtures.json).
    touched = {p for p, js in fixture_reviewers().items() if "jdg_04" in js}
    assert touched == {"prj_04", "prj_14", "prj_18", "prj_37"}
    with portal(tmp_path) as c:
        before = results_rows(c)
        r = exclude(c, "jdg_04", "harsh outlier, see agreement report")
        assert r.status_code == 303
        after = results_rows(c)
        assert set(after) == set(before)
        for p in before:
            if p in touched:
                assert int(after[p]["n_reviews"]) == int(before[p]["n_reviews"]) - 1, p
            else:
                # Edge: exclusion changes judge statistics for nobody else, so projects jdg_04 did
                # not review keep their raw mean and normalized z exactly.
                assert (after[p]["n_reviews"], after[p]["raw_mean"], after[p]["normalized_z"]) == \
                       (before[p]["n_reviews"], before[p]["raw_mean"], before[p]["normalized_z"]), p
        rows = audit_rows(c, "judge.exclude")
        assert rows == [("jdg_04", {"reason": "harsh outlier, see agreement report"})]
        # Dashboard lists the exclusion (with its reason, organizer-only).
        page = c.get("/organizer", headers=ORGANIZER)
        assert page.status_code == 200 and "harsh outlier, see agreement report" in page.text
        api = c.get("/api/judge-agreement", headers=ORGANIZER).json()
        assert next(x for x in api["judges"] if x["judge"] == "jdg_04")["excluded"] is True
        # Reviews are never deleted.
        n = c.app.state.conn.execute("SELECT COUNT(*) FROM reviews WHERE judge_id = 'jdg_04'").fetchone()[0]
        assert n == 4


def test_case8_exclusion_reaches_confidence_and_edge_every_judge_of_a_project(tmp_path):
    # prj_18 was reviewed only by jdg_04 and jdg_24 (fixtures.json). Excluding both leaves it with
    # zero reviews: ranked last with low_confidence; confidence says unreviewed, p 0.000.
    assert fixture_reviewers()["prj_18"] == {"jdg_04", "jdg_24"}
    with portal(tmp_path) as c:
        assert conf_rows(c)["prj_18"]["unreviewed"] == "false"
        assert exclude(c, "jdg_04").status_code == 303
        assert exclude(c, "jdg_24").status_code == 303
        res = results_rows(c)
        row18 = res["prj_18"]
        assert row18["n_reviews"] == "0" and row18["raw_mean"] == "" and row18["low_confidence"] == "true"
        reviewed_ranks = [int(x["rank"]) for x in res.values() if x["n_reviews"] != "0"]
        assert int(row18["rank"]) > max(reviewed_ranks)
        conf = conf_rows(c)["prj_18"]
        assert (conf["unreviewed"], conf["p_top_k"], conf["close_call"]) == ("true", "0.000", "false")


def test_case9_empty_reason_422(tmp_path):
    with portal(tmp_path) as c:
        for body in ({"reason": ""}, {"reason": "   "}, {}, {"reason": None}, {"reason": 5}, {"reason": ["x"]}):
            r = c.post("/organizer/judges/jdg_04/exclude", json=body, headers=ORGANIZER)
            assert r.status_code == 422, body
        r = c.post("/organizer/judges/jdg_04/exclude", data={"reason": ""}, headers=ORGANIZER)
        assert r.status_code == 422
        assert audit_rows(c, "judge.exclude") == []
        assert c.app.state.conn.execute("SELECT COUNT(*) FROM judge_exclusions").fetchone()[0] == 0


def test_case10_reinclude_restores_exactly(tmp_path):
    with portal(tmp_path) as c:
        res0 = c.get("/api/export.csv", headers=ORGANIZER).content
        conf0 = c.get("/api/confidence.csv", headers=ORGANIZER).content
        agr0 = c.get("/api/judge-agreement", headers=ORGANIZER).json()
        assert exclude(c, "jdg_04").status_code == 303
        assert exclude(c, "jdg_26").status_code == 303
        assert c.get("/api/export.csv", headers=ORGANIZER).content != res0
        for j in ("jdg_04", "jdg_26"):
            assert c.post(f"/organizer/judges/{j}/include", headers=ORGANIZER).status_code == 303
        assert c.get("/api/export.csv", headers=ORGANIZER).content == res0
        assert c.get("/api/confidence.csv", headers=ORGANIZER).content == conf0
        assert c.get("/api/judge-agreement", headers=ORGANIZER).json() == agr0
        assert [s for s, _ in audit_rows(c, "judge.include")] == ["jdg_04", "jdg_26"]


def test_case11_public_results_show_count_only(tmp_path):
    reason = "conflict disclosed after judging"
    with portal(tmp_path) as c:
        assert c.get("/results").status_code == 404  # not published yet
        assert c.post("/organizer/publish", json={}, headers=ORGANIZER).status_code == 303
        page = c.get("/results")
        assert page.status_code == 200
        assert "were excluded by the organizer" not in page.text     # no exclusion yet
        assert exclude(c, "jdg_04", reason).status_code == 303
        page = c.get("/results").text
        assert re.search(r"Reviews from 1 judge(\(s\))? were excluded by the organizer", page)
        for secret in ("jdg_04", reason, "Noor Haddad", "noor.haddad@example.org"):
            assert secret not in page, secret
        assert exclude(c, "jdg_26", reason).status_code == 303
        page = c.get("/results").text
        assert re.search(r"Reviews from 2 judge(s|\(s\)) were excluded by the organizer", page)
        assert "jdg_26" not in page and "Jonas Vogel" not in page


@pytest.mark.parametrize("headers,expected", [(JUDGE_A, 403), (PARTICIPANT, 403), ({}, 401)],
                         ids=["judge", "participant", "visitor"])
def test_case12_authz_report_and_exclusion(tmp_path, headers, expected):
    with portal(tmp_path) as c:
        r = c.get("/api/judge-agreement", headers=headers)
        assert r.status_code == expected
        for path in ("/organizer/judges/jdg_04/exclude", "/organizer/judges/jdg_04/include"):
            r = c.post(path, json={"reason": "x"}, headers={**headers, **JSON})
            assert r.status_code == expected, path
        assert c.app.state.conn.execute("SELECT COUNT(*) FROM judge_exclusions").fetchone()[0] == 0
        assert audit_rows(c, "judge.exclude") == []


def test_exclusion_of_unknown_or_other_event_judge_is_404(tmp_path):
    with portal(tmp_path) as c:
        r = c.post("/organizer/judges/jdg_999/exclude", json={"reason": "x"}, headers={**ORGANIZER, **JSON})
        assert r.status_code == 404
        # A second event with its own judge (the demo organizer is an admin, lifecycle.md).
        r = c.post("/organizer/events", json={"name": "Other", "submissions_close": "2099-01-01T00:00:00Z"},
                   headers=ORGANIZER)
        assert r.status_code == 303
        other = r.headers["location"].split("event=")[1]
        r = c.post(f"/organizer/judges?event={other}", json={"email": "other.judge@example.org"}, headers=ORGANIZER)
        assert r.status_code == 303
        jid = c.app.state.conn.execute("SELECT id FROM judges WHERE event_id = ?", (other,)).fetchone()[0]
        # Excluding it while acting on evt_01 must not reach across events.
        r = c.post(f"/organizer/judges/{jid}/exclude?event=evt_01", json={"reason": "x"},
                   headers={**ORGANIZER, **JSON})
        assert r.status_code == 404
        # And an evt_01 judge cannot be excluded through the other event.
        r = c.post(f"/organizer/judges/jdg_04/exclude?event={other}", json={"reason": "x"},
                   headers={**ORGANIZER, **JSON})
        assert r.status_code == 404
        assert c.app.state.conn.execute("SELECT COUNT(*) FROM judge_exclusions").fetchone()[0] == 0


def test_excluding_every_judge_never_500(tmp_path):
    judges = [j["id"] for j in load_fixture_json()["judges"]]
    assert len(judges) == 30
    with portal(tmp_path) as c:
        for j in judges:
            assert exclude(c, j).status_code == 303, j
        res = results_rows(c)
        assert len(res) == 40
        assert all(x["n_reviews"] == "0" and x["low_confidence"] == "true" for x in res.values())
        conf = conf_rows(c)
        assert all(x["unreviewed"] == "true" and x["p_top_k"] == "0.000" for x in conf.values())
        assert c.get("/organizer", headers=ORGANIZER).status_code == 200
        assert c.get("/api/judge-agreement", headers=ORGANIZER).status_code == 200
        assert c.post("/organizer/tiebreak", json={"k": 3}, headers=ORGANIZER).status_code == 303
        tb = audit_rows(c, "assignment.tiebreak")
        assert tb[-1][1]["added"] == []
        assert c.post("/organizer/publish", json={}, headers=ORGANIZER).status_code == 303
        page = c.get("/results")
        assert page.status_code == 200
        assert re.search(r"Reviews from 30 judge(s|\(s\)) were excluded by the organizer", page.text)


def test_exclusion_reason_is_escaped_on_dashboard(tmp_path):
    with portal(tmp_path) as c:
        assert exclude(c, "jdg_04", "<script>alert(1)</script>").status_code == 303
        page = c.get("/organizer", headers=ORGANIZER).text
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


def test_agreement_report_uses_all_reviews_including_excluded(tmp_path):
    # The organizer must still see the evidence behind an exclusion (services.analysis docstring,
    # contract "Excluded reviews are never deleted"): jdg_04's row keeps its shared count.
    with portal(tmp_path) as c:
        before = next(x for x in c.get("/api/judge-agreement", headers=ORGANIZER).json()["judges"]
                      if x["judge"] == "jdg_04")
        assert exclude(c, "jdg_04").status_code == 303
        after = next(x for x in c.get("/api/judge-agreement", headers=ORGANIZER).json()["judges"]
                     if x["judge"] == "jdg_04")
        assert {k: v for k, v in after.items() if k != "excluded"} == \
               {k: v for k, v in before.items() if k != "excluded"}


def test_case8_dashboard_lists_exclusion_of_judge_without_reviews(tmp_path):
    # Case 8: "dashboard lists the exclusion" -- also for a judge who has no reviews yet, and it
    # must be reversible from the dashboard (a re-include control for that judge).
    with portal(tmp_path) as c:
        r = c.post("/organizer/judges", json={"email": "fresh.judge@example.org", "tracks": ["trk_01"]},
                   headers=ORGANIZER)
        assert r.status_code == 303
        jid = c.app.state.conn.execute("SELECT j.id FROM judges j JOIN users u ON u.id = j.user_id "
                                       "WHERE u.email = 'fresh.judge@example.org'").fetchone()[0]
        assert exclude(c, jid, "never showed up to the briefing").status_code == 303
        page = c.get("/organizer", headers=ORGANIZER).text
        assert "never showed up to the briefing" in page
        assert f"/organizer/judges/{jid}/include" in page
