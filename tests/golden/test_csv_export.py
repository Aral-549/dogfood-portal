"""contracts/csv-export.md cases 1-6 and edge cases, pure (dogfood.core.csvexport) and HTTP."""

import csv
import io

import pytest

from conftest import ORGANIZER, load_fixture_json, portal
from dogfood.core.csvexport import results_csv
from dogfood.core.scoring import ProjectResult, Review, score

HEADER = "rank,project_id,title,team,track,n_reviews,raw_mean,normalized_z,low_confidence"


def one_row(result, title="T", team="Team", track="Track"):
    text = results_csv([result], {result.project: {"title": title, "team": team, "track": track}})
    lines = text.split("\r\n")
    return text, lines


def parsed(text):
    return list(csv.reader(io.StringIO(text, newline="")))


def result(**kw):
    base = dict(project="P1", n_reviews=3, raw_mean=3.0, z=0.0, rank=1, raw_rank=1, low_confidence=False)
    base.update(kw)
    return ProjectResult(**base)


# --- case 2 ---------------------------------------------------------------------------
def test_case2_four_decimal_places():
    text, lines = one_row(result(raw_mean=3.666666))
    assert lines[0] == HEADER
    assert parsed(text)[1][6] == "3.6667"


@pytest.mark.parametrize("value,expected", [(4.0, "4.0000"), (2.5, "2.5000"), (0.72686, "0.7269")])
def test_case2_fixed_width(value, expected):
    text, _ = one_row(result(raw_mean=value, z=value))
    row = parsed(text)[1]
    assert row[6] == expected and row[7] == expected


# --- case 3 ---------------------------------------------------------------------------
def test_case3_zero_reviews_empty_numbers_low_confidence_true():
    # From scoring.md case 5: P3 has no reviews -> raw_mean None, z None, low_confidence true.
    reviews = [Review("A", "P1", {"q": 2}), Review("A", "P2", {"q": 4}),
               Review("B", "P1", {"q": 5}), Review("B", "P2", {"q": 5})]
    res = score({"q": 1}, reviews, ["P1", "P2", "P3"])
    meta = {p: {"title": p, "team": "t", "track": "k"} for p in ("P1", "P2", "P3")}
    rows = {r[1]: r for r in parsed(results_csv(res, meta))[1:]}
    assert rows["P3"][5] == "0"
    assert rows["P3"][6] == ""
    assert rows["P3"][7] == ""
    assert rows["P3"][8] == "true"
    assert rows["P3"][0] == "3"


# --- case 4 ---------------------------------------------------------------------------
def test_case4_quoting():
    text, lines = one_row(result(), title='Dry, Harbour "v2"')
    assert '"Dry, Harbour ""v2"""' in lines[1]
    assert parsed(text)[1][2] == 'Dry, Harbour "v2"'


# --- case 5 ---------------------------------------------------------------------------
@pytest.mark.parametrize("title", ["=1+1", "+1", "-1", "@SUM(A1)", "\t=1", "\r=1", "=HYPERLINK(\"x\")"])
def test_case5_formula_injection_prefixed(title):
    text, _ = one_row(result(), title=title)
    assert parsed(text)[1][2] == "'" + title


@pytest.mark.parametrize("title", ["Glass Signal", "Café", "1st place", "'quoted"])
def test_case5_ordinary_titles_untouched(title):
    text, _ = one_row(result(), title=title)
    assert parsed(text)[1][2] == title


def test_case5_team_and_track_also_defended():
    text, _ = one_row(result(), team="=cmd", track="@x")
    row = parsed(text)[1]
    assert row[3] == "'=cmd" and row[4] == "'@x"


# --- case 6 and edges ---------------------------------------------------------------------
def test_edge_embedded_newline_stays_in_one_field():
    text, _ = one_row(result(), title="line1\nline2")
    rows = parsed(text)
    assert len(rows) == 2
    assert rows[1][2] == "line1\nline2"


def test_crlf_line_endings():
    text, _ = one_row(result())
    assert text.endswith("\r\n")
    assert text.count("\r\n") == 2


# --- HTTP -----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def http(tmp_path_factory):
    with portal(tmp_path_factory.mktemp("csv_http")) as c:
        r = c.get("/api/export.csv", headers=ORGANIZER)
        # Empty event for the "header only" edge: created by the demo organizer (admin).
        made = c.post("/organizer/events", data={"name": "Empty", "submissions_close": "2030-01-01T00:00:00Z"},
                      headers=ORGANIZER)
        loc = made.headers.get("location", "")
        eid = loc.split("event=", 1)[1] if "event=" in loc else None
        empty = c.get(f"/api/export.csv?event={eid}", headers=ORGANIZER) if eid else None
        yield r, empty


def test_http_case1_header_and_40_rows_by_rank(http):
    r, _ = http
    assert r.status_code == 200
    lines = r.content.decode("utf-8").split("\r\n")
    assert lines[0] == HEADER
    rows = parsed(r.content.decode("utf-8"))[1:]
    assert len(rows) == 40
    assert [int(x[0]) for x in rows] == list(range(1, 41))
    ids = {x[1] for x in rows}
    assert "prj_07" not in ids
    canonical = {p["id"] for p in load_fixture_json()["projects"]} - {"prj_07"}
    assert ids == canonical


def test_http_content_type_and_disposition(http):
    r, _ = http
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == 'attachment; filename="results-evt_01.csv"'


def test_http_case6_utf8_no_bom(http):
    r, _ = http
    assert not r.content.startswith(b"\xef\xbb\xbf")
    r.content.decode("utf-8")


def test_http_edge_empty_event_header_only(http):
    _, empty = http
    assert empty is not None
    assert empty.status_code == 200
    assert empty.content.decode("utf-8") == HEADER + "\r\n"
