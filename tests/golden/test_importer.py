"""contracts/fixtures-import.md cases 1-3, 6-8, 13.

Expected counts are the contract's numbers, cross-checked by counting fixtures.json
directly with the stdlib (never by reading the importer's own report back as truth).
"""

import copy
from datetime import datetime, timezone

import pytest

from conftest import load_fixture_json, portal
from dogfood import db
from dogfood.core import importer

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


def fresh_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def count(conn, table, where="1=1", args=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def table_counts(conn):
    return {t: count(conn, t) for t in ("events", "tracks", "judges", "teams", "team_members", "projects",
                                        "reviews", "review_scores", "users", "criteria", "assignments")}


@pytest.fixture(scope="module")
def data():
    return load_fixture_json()


@pytest.fixture(scope="module")
def imported(data):
    conn = fresh_conn()
    report = importer.import_fixtures(conn, copy.deepcopy(data), NOW, "secret")
    return conn, report


# --- case 1 ---------------------------------------------------------------------------
def test_case1_file_counts_match_contract(data):
    # Independent count of the raw file, so a changed upstream file is noticed here first.
    assert (len(data["tracks"]), len(data["judges"]), len(data["teams"]), len(data["projects"]),
            len(data["scores"])) == (8, 30, 40, 41, 126)


def test_case1_row_counts(imported):
    conn, report = imported
    assert count(conn, "events") == 1
    assert count(conn, "tracks") == 8
    assert count(conn, "judges") == 30
    assert count(conn, "teams") == 40
    assert count(conn, "projects") == 41
    assert count(conn, "reviews") == 126
    # 126 reviews x 3 rubric criteria (functionality, quality, innovation)
    assert count(conn, "review_scores") == 126 * 3
    assert report.rejected == []


# --- case 2 ---------------------------------------------------------------------------
def test_case2_import_twice_is_idempotent(data):
    conn = fresh_conn()
    importer.import_fixtures(conn, copy.deepcopy(data), NOW, "secret")
    first = table_counts(conn)
    report = importer.import_fixtures(conn, copy.deepcopy(data), NOW, "secret")
    assert table_counts(conn) == first
    assert report.unchanged is True
    assert report.rejected == []


def test_case2_boot_twice_same_data_dir(tmp_path):
    # "idempotent on restart": two boots over one data dir leave the same rows.
    with portal(tmp_path) as c:
        before = c.get("/api/projects").json()["total"]
        csv1 = c.get("/api/export.csv", headers={"Authorization": "Bearer demo-organizer"}).text
    with portal(tmp_path) as c:
        assert c.get("/api/projects").json()["total"] == before == 40
        csv2 = c.get("/api/export.csv", headers={"Authorization": "Bearer demo-organizer"}).text
    assert csv1 == csv2


# --- case 3 ---------------------------------------------------------------------------
def test_case3_close_stored_exactly(imported):
    conn, _ = imported
    assert conn.execute("SELECT submissions_close FROM events WHERE id = 'evt_01'").fetchone()[0] == \
        "2026-03-01T18:00:00Z"


def test_case3_close_not_replaced_in_demo_mode(tmp_path):
    with portal(tmp_path, demo=True):
        pass
    conn = db.connect(str(tmp_path / "dogfood.db"))
    assert conn.execute("SELECT submissions_close FROM events WHERE id = 'evt_01'").fetchone()[0] == \
        "2026-03-01T18:00:00Z"
    conn.close()


# --- case 6 ---------------------------------------------------------------------------
def test_case6_duplicate_pair_flagged(imported):
    conn, report = imported
    rows = {r[0]: r[1] for r in conn.execute("SELECT id, superseded_by FROM projects WHERE id IN ('prj_07','prj_41')")}
    assert rows == {"prj_07": "prj_41", "prj_41": None}
    pairs = [(d["superseded"], d["canonical"]) for d in report.duplicates]
    assert pairs == [("prj_07", "prj_41")]


def test_case6_no_other_project_superseded(imported):
    conn, _ = imported
    # The file has exactly one team with two projects (tm_07); count it independently.
    data = load_fixture_json()
    teams = [p["team"] for p in data["projects"]]
    assert [t for t in set(teams) if teams.count(t) > 1] == ["tm_07"]
    assert count(conn, "projects", "superseded_by IS NOT NULL") == 1


def test_case6_raw_reviews_kept_on_original_project(imported):
    conn, _ = imported
    # README decision 1: raw review rows stay on their project; the merge is computed.
    data = load_fixture_json()
    n07 = sum(1 for s in data["scores"] if s["project"] == "prj_07")
    assert n07 == 5
    assert count(conn, "reviews", "project_id = 'prj_07'") == n07


# --- case 7 ---------------------------------------------------------------------------
def test_case7_merged_review_count_in_results(tmp_path):
    # Merged set = 6 reviewers (jdg_19, jdg_21, jdg_26, jdg_01, jdg_12, jdg_18).
    import csv
    import io
    with portal(tmp_path) as c:
        body = c.get("/api/export.csv", headers={"Authorization": "Bearer demo-organizer"}).text
    rows = {r["project_id"]: r for r in csv.DictReader(io.StringIO(body))}
    assert "prj_07" not in rows
    assert rows["prj_41"]["n_reviews"] == "6"


# --- case 8 ---------------------------------------------------------------------------
def test_case8_gallery_shows_canonical_hides_superseded(tmp_path):
    with portal(tmp_path) as c:
        ids = [p["id"] for p in c.get("/api/projects").json()["items"]]
        html = c.get("/projects").text
        search = [p["id"] for p in c.get("/api/projects", params={"q": "Dry Harbour"}).json()["items"]]
    assert "prj_41" in ids and "prj_07" not in ids
    assert len(ids) == 40
    assert "/projects/prj_41" in html and "/projects/prj_07" not in html
    assert search == ["prj_41"]


# --- case 13 --------------------------------------------------------------------------
def test_case13_empty_comment_stored_as_empty_string(imported, data):
    conn, _ = imported
    empty_in_file = sum(1 for s in data["scores"] if s.get("comment", None) == "")
    assert empty_in_file == 51
    assert count(conn, "reviews", "comment = ''") == 51
    assert count(conn, "reviews", "comment IS NULL") == 0
