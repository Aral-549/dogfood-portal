"""Features adopted from research on other hackathon platforms (RESEARCH.md), 2026-09-27.

Real app over HTTP, like the rest of the suite. Fixture facts: demo judge A is jdg_24, assigned
and scored on 11 projects including prj_06.
"""

import sqlite3

from conftest import JUDGE_A, JUDGE_B, ORGANIZER, portal

ALL_THREES = {"functionality": 3, "quality": 3, "innovation": 3}


def _q(data_dir, sql, args=()):
    conn = sqlite3.connect(str(data_dir / "dogfood.db"))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# --- recusal (Devpost: judges recuse themselves from conflicted submissions) ----------------
def test_recusal_removes_assignment_score_and_future_pairing(tmp_path):
    with portal(tmp_path) as c:
        url = "/api/v1/judge/recusals/prj_06"
        assert c.post(url, json={"reason": " "}, headers=JUDGE_A).status_code == 422
        assert c.post(url, json={"reason": "my cousin is on this team"}, headers=JUDGE_A).status_code == 204
        assert _q(tmp_path, "SELECT 1 FROM assignments WHERE judge_id='jdg_24' AND project_id='prj_06'") == []
        assert _q(tmp_path, "SELECT COUNT(*) FROM audit_log WHERE action='judge.recuse'")[0][0] == 1
        # the review row is kept (audit trail) but no longer counts
        assert _q(tmp_path, "SELECT COUNT(*) FROM reviews WHERE judge_id='jdg_24' AND project_id='prj_06'")[0][0] == 1
        after = {r["project"]: r for r in c.get("/api/v1/results", headers=ORGANIZER).json()}
        assert after["prj_06"]["n_reviews"] == _q(
            tmp_path, "SELECT COUNT(*) FROM reviews WHERE project_id='prj_06'")[0][0] - 1
        # cannot score it again, and auto-assign never gives it back
        r = c.post("/api/v1/judge/scores/prj_06", json=ALL_THREES, headers=JUDGE_A)
        assert r.status_code == 403
        assert c.post("/api/v1/assignments/auto", json={"k": 10}, headers=ORGANIZER).status_code == 303
        assert _q(tmp_path, "SELECT 1 FROM assignments WHERE judge_id='jdg_24' AND project_id='prj_06'") == []
        assert "my cousin is on this team" in c.get("/organizer", headers=ORGANIZER).text


def test_recusal_only_for_own_assignments(tmp_path):
    with portal(tmp_path) as c:
        own_b = _q(tmp_path, "SELECT project_id FROM assignments WHERE judge_id='jdg_26'")
        not_a = [p for (p,) in own_b if not _q(
            tmp_path, "SELECT 1 FROM assignments WHERE judge_id='jdg_24' AND project_id=?", (p,))]
        assert c.post(f"/api/v1/judge/recusals/{not_a[0]}", json={"reason": "x"}, headers=JUDGE_A).status_code == 404
        assert c.post("/api/v1/judge/recusals/prj_06", json={"reason": "x"}).status_code == 401
        assert c.post("/api/v1/judge/recusals/prj_06", json={"reason": "x"}, headers=ORGANIZER).status_code == 404


def test_recusals_survive_export_import(tmp_path):
    with portal(tmp_path) as c:
        c.post("/api/v1/judge/recusals/prj_06", json={"reason": "friend"}, headers=JUDGE_A)
        exported = c.get("/api/v1/events/evt_01/export.json", headers=ORGANIZER).json()
    assert exported["recusals"] == [{"judge": "jdg_24", "project": "prj_06", "reason": "friend",
                                     "at": exported["recusals"][0]["at"]}]


# --- ordering-only ranking (Gavel / Crowd-BT, MadHacks Plackett-Luce, MLH stack ranking) -------
from dogfood.core.ordinal import bradley_terry, comparisons  # noqa: E402
from dogfood.core.scoring import ScoredReview  # noqa: E402


def _s(judge, project, r):
    return ScoredReview(judge, project, float(r), 0.0)


def test_comparisons_are_within_judge_only_with_half_wins_for_ties():
    got = comparisons([_s("j1", "a", 5), _s("j1", "b", 3), _s("j1", "c", 3), _s("j2", "a", 1), _s("j2", "d", 4)])
    assert got == [("a", "b", 1.0), ("a", "c", 1.0), ("b", "c", 0.5), ("a", "d", 0.0)]


def test_two_items_one_game_matches_hand_solution():
    # a beat b once; each also ties the virtual opponent (strength 1). MM fixed point:
    #   p_a (1/(p_a+p_b) + 1/(p_a+1)) = 1.5,   p_b (1/(p_a+p_b) + 1/(p_b+1)) = 0.5.
    # Adding them: p_a/(p_a+1) + p_b/(p_b+1) = 1, i.e. p_a p_b = 1. Substituting p_b = 1/x into the
    # first: x^2/(x^2+1) + x/(x+1) = 3/2  ->  x^3 - x^2 - x - 3 = 0, whose real root is 2.13039...
    res = bradley_terry([_s("j", "a", 5), _s("j", "b", 1)], ["a", "b"])
    x = res.strengths["a"]
    assert abs(x ** 3 - x ** 2 - x - 3) < 1e-9 and abs(x - 2.130395435) < 1e-8
    assert abs(res.strengths["a"] * res.strengths["b"] - 1) < 1e-9
    assert res.ranks == {"a": 1, "b": 2} and res.comparisons == 1


def test_generosity_does_not_matter_only_order_does():
    # j2 scores everything 3 points higher than j1 would; the ordering-only ranking is identical
    # to the one where j2 used j1's scale. (The raw mean ranking is not.)
    base = [_s("j1", "a", 2), _s("j1", "b", 1), _s("j2", "b", 1), _s("j2", "c", 2), _s("j3", "a", 1), _s("j3", "c", 2)]
    lifted = [ScoredReview(s.judge, s.project, s.r + (3 if s.judge == "j2" else 0), 0.0) for s in base]
    assert bradley_terry(base, "abc").ranks == bradley_terry(lifted, "abc").ranks


def test_cycle_is_a_three_way_tie_broken_by_id_and_unscored_last():
    rows = [_s("j1", "a", 2), _s("j1", "b", 1), _s("j2", "b", 2), _s("j2", "c", 1), _s("j3", "c", 2), _s("j3", "a", 1)]
    res = bradley_terry(rows, ["z", "c", "b", "a"])
    assert all(abs(v - 1.0) < 1e-9 for v in res.strengths.values())
    assert res.ranks == {"a": 1, "b": 2, "c": 3, "z": 4}


def test_cross_check_endpoint_is_organizer_only_and_flags_disputes(tmp_path):
    with portal(tmp_path) as c:
        assert c.get("/api/v1/results/cross-check", headers=JUDGE_A).status_code == 403
        assert c.get("/api/v1/results/cross-check").status_code == 401
        body = c.get("/api/v1/results/cross-check?k=3", headers=ORGANIZER).json()
        assert body["k"] == 3 and len(body["projects"]) == 40
        by = {x["project"]: x for x in body["projects"]}
        assert sorted(x["ordering_rank"] for x in body["projects"]) == list(range(1, 41))
        for x in body["projects"]:
            inside = {x["rank"] <= 3, x["shrunk_rank"] <= 3, x["ordering_rank"] <= 3}
            assert x["prize_line_disputed"] == (len(inside) > 1)
        assert by["prj_34"]["prize_line_disputed"] is False            # 1st by every method
        page = c.get("/organizer", headers=ORGANIZER).text
        assert "Order rank" in page and "prize line disputed" in page


# --- pre-announcement integrity checks (MLH: "cheating check on all winners") -------------------
from dogfood.core import integrity  # noqa: E402

LONG = "A realtime dashboard that tracks air quality sensors across campus and alerts students"


def test_normalize_repo():
    assert integrity.normalize_repo("https://www.GitHub.com/Org/Repo.git/") == "github.com/org/repo"
    assert integrity.normalize_repo("http://github.com/org/repo") == "github.com/org/repo"
    assert integrity.normalize_repo("") == "" and integrity.normalize_repo("not a url") == ""


def test_similarity_hand_computed():
    # "a b c d" -> {abc, bcd}; "a b c e" -> {abc, bce}: Jaccard 1/3.
    assert abs(integrity.jaccard(integrity.shingles("a b c d"), integrity.shingles("a b c e")) - 1 / 3) < 1e-12


def test_checks():
    projects = [
        {"id": "p1", "team": "t1", "title": "Air", "summary": LONG, "repo_url": "https://github.com/x/air"},
        {"id": "p2", "team": "t2", "title": "Air", "summary": LONG + " daily", "repo_url": "https://github.com/X/air.git"},
        {"id": "p3", "team": "t1", "title": "Air v2", "summary": LONG, "repo_url": "https://github.com/x/air"},
        {"id": "p4", "team": "t3", "title": "Short", "summary": "One line of what it does.", "repo_url": ""},
        {"id": "p5", "team": "t4", "title": "Short", "summary": "One line of what it does.", "repo_url": "https://gitlab.com/y/z"},
    ]
    got = {(f.project, f.kind, f.other) for f in integrity.check(projects)}
    assert ("p1", "shared_repo", "p2") in got and ("p2", "shared_repo", "p1") in got
    assert ("p1", "similar_text", "p2") in got
    assert ("p1", "shared_repo", "p3") not in got          # same team: the importer's duplicate merge, not a flag
    assert ("p4", "no_repo", None) in got
    assert not any(f[1] == "similar_text" and "p4" in (f[0], f[2]) for f in got)   # boilerplate one-liners


def test_integrity_endpoint_puts_contenders_first(tmp_path):
    with portal(tmp_path) as c:
        assert c.get("/api/v1/integrity", headers=JUDGE_A).status_code == 403
        assert c.get("/api/v1/integrity").status_code == 401
        assert c.get("/api/v1/integrity?k=3", headers=ORGANIZER).json()["flags"] == []   # fixture is clean
        # A copy of the winner's repository submitted by another team.
        top = c.get("/api/v1/results", headers=ORGANIZER).json()[0]["project"]
        repo = _q(tmp_path, "SELECT repo_url FROM projects WHERE id = ?", (top,))[0][0]
        c.post("/api/v1/event", json={"submissions_close": "2099-01-01T00:00:00Z"}, headers=ORGANIZER)
        c.post("/register", data={"email": "copycat@example.org", "password": "a-long-password"})
        tok = c.post("/api/v1/tokens", json={}).json()["token"]
        c.cookies.clear()
        cat = {"Authorization": f"Bearer {tok}"}
        c.post("/api/v1/teams", json={"name": "Copycats"}, headers=cat)
        assert c.post("/api/v1/projects", json={"title": "Totally new", "repo_url": repo}, headers=cat).status_code == 201
        flags = c.get("/api/v1/integrity?k=3", headers=ORGANIZER).json()["flags"]
        assert flags[0]["project"] == top and flags[0]["kind"] == "shared_repo" and flags[0]["prize_contender"]
        assert "flag on prize contenders" in c.get("/organizer", headers=ORGANIZER).text


# --- capacity planning (MLH judge sizing formula) and per-track winners (Devpost categories) ------
def test_plan_matches_mlh_formula():
    from dogfood.core.planning import plan
    # MLH's own reference row: 175 projects, 3 rounds, 4 minutes, 120 minutes -> 18 judges
    # (175 * 3 * 4 / 120 = 17.5, rounded up).
    p = plan(175, 18)
    assert p.judges_needed_for[120] == 18
    assert p.reviews_needed == 525 and p.per_judge == 30 and p.minutes_per_judge == 120
    assert plan(10, 0).per_judge == 0


def test_dashboard_plan_and_results_by_track(tmp_path):
    with portal(tmp_path) as c:
        page = c.get("/organizer", headers=ORGANIZER).text
        # fixture: 40 canonical projects x 3 = 120 reviews needed; 30 judges -> 4 each, 16 min
        assert "40 projects &times; 3 reviews = 120 reviews needed" in page
        assert "each does about 4 (16 min" in page
        c.post("/api/v1/results/publish", json={}, headers=ORGANIZER)
        res = c.get("/results").text
        assert "Top of each track" in res and "overall #1" in res
