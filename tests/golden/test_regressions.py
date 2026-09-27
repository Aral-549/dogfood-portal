"""Regression cases for bugs found in the 2026-09-27 adversarial pass (see BUGLOG.md).

Each test names the contract clause it enforces, or says "uncontracted" where the
expectation is the generic "malformed input must not produce a 500".
These are expected to FAIL until the matching BUGLOG entry is fixed.
"""

import copy
from datetime import datetime, timezone

import pytest

from conftest import ORGANIZER, load_fixture_json, portal
from dogfood import db
from dogfood.core import importer
from dogfood.core.csvexport import results_csv
from dogfood.core.scoring import Review, normalize_weights, score

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


def fresh_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


# BUG-1 -- lifecycle.md edge "demo accounts only when DOGFOOD_DEMO_SESSIONS=1";
# authz.md case 14 ("demo tokens only exist when explicitly enabled").
def test_bug1_demo_password_dead_after_demo_mode_disabled(tmp_path):
    with portal(tmp_path, demo=True):
        pass
    with portal(tmp_path, demo=False) as c:
        r = c.post("/login", data={"email": "organizer@dogfood.local", "password": "dogfood-demo"})
        assert r.status_code == 401
        r = c.post("/login", data={"email": "diego.herrera@example.org", "password": "dogfood-demo"})
        assert r.status_code == 401


# BUG-2 -- fixtures-import.md case 10 ("row rejected with reason, import continues") and
# edge "Rubric criteria are derived from the fixture (functionality, quality, innovation)".
def test_bug2_one_misspelled_criterion_rejects_only_that_row():
    data = load_fixture_json()
    bad = copy.deepcopy(data)
    crit = bad["scores"][0]["criteria"]
    crit["qualty"] = crit.pop("quality")
    conn = fresh_conn()
    report = importer.import_fixtures(conn, bad, NOW, "secret")
    # 126 rows in the file, 1 broken -> 125 imported, 1 rejected.
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 125
    assert len(report.rejected) == 1
    names = {r[0] for r in conn.execute("SELECT name FROM criteria WHERE event_id = 'evt_01'")}
    assert names == {"functionality", "quality", "innovation"}


def test_bug2_rejected_row_with_extra_criterion_does_not_poison_rubric():
    data = load_fixture_json()
    bad = copy.deepcopy(data)
    bad["scores"].append({"judge": "jdg_999", "project": "prj_01", "comment": "",
                          "criteria": {"functionality": 3, "quality": 3, "innovation": 3, "bonus": 3}})
    conn = fresh_conn()
    importer.import_fixtures(conn, bad, NOW, "secret")
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 126


# BUG-3 -- fixtures-import.md case 11 ("score referencing unknown judge or project: row rejected
# with reason") and the "import continues" rule of cases 9-10.
@pytest.mark.parametrize("field,value", [("judge", ["jdg_01"]), ("project", {"id": "prj_01"})])
def test_bug3_non_string_reference_is_rejected_not_fatal(field, value):
    bad = copy.deepcopy(load_fixture_json())
    bad["scores"][0][field] = value
    conn = fresh_conn()
    report = importer.import_fixtures(conn, bad, NOW, "secret")
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 125
    assert len(report.rejected) == 1


# BUG-4 -- csv-export.md case 2 (4 dp, fixed). A mathematically zero z printed as "-0.0000".
def test_bug4_zero_z_is_not_printed_negative():
    # Criteria f, q, i, equal weights 1/3. Two judges, three projects.
    # J0: f 2,2,3 (mu 7/3, sd sqrt(2)/3)  q 1,2,3 (mu 2, sd sqrt(2/3))  i 5,5,1 (mu 11/3, sd 4*sqrt(2)/3)
    # J1: f 2,2,1 (mu 5/3, sd sqrt(2)/3)  q 5,3,4 (mu 4, sd sqrt(2/3))  i 1,1,4 (mu 2,    sd sqrt(2))
    # P0 by J0: z_f = -1/sqrt2, z_q = -sqrt(3/2), z_i = +1/sqrt2 -> sum -sqrt(3/2)
    # P0 by J1: z_f = +1/sqrt2, z_q = +sqrt(3/2), z_i = -1/sqrt2 -> sum +sqrt(3/2)
    # mean over the two reviews: exactly 0.
    reviews = [
        Review("J0", "P0", {"f": 2, "q": 1, "i": 5}), Review("J0", "P1", {"f": 2, "q": 2, "i": 5}),
        Review("J0", "P2", {"f": 3, "q": 3, "i": 1}), Review("J1", "P0", {"f": 2, "q": 5, "i": 1}),
        Review("J1", "P1", {"f": 2, "q": 3, "i": 1}), Review("J1", "P2", {"f": 1, "q": 4, "i": 4}),
    ]
    res = {r.project: r for r in score({"f": 1, "q": 1, "i": 1}, reviews, ["P0", "P1", "P2"])}
    assert res["P0"].z == pytest.approx(0.0, abs=1e-9)
    line = results_csv([res["P0"]], {"P0": {"title": "t", "team": "t", "track": "t"}}).split("\r\n")[1]
    assert line.split(",")[7] == "0.0000"


# BUG-5 -- scoring.md method step 1 (w_c = weight_c / sum(weights)) and case 7.
def test_bug5_large_valid_weights_do_not_collapse_to_zero():
    # 1.7e308 / (1.7e308 + 1.7e308) = 0.5 each, mathematically.
    w = normalize_weights({"a": 1.7e308, "b": 1.7e308})
    assert w["a"] == pytest.approx(0.5) and w["b"] == pytest.approx(0.5)


# BUG-6 -- uncontracted: malformed input must be a 4xx, never a 500.
MALFORMED = [
    ("GET", "/projects?page=99999999999999999999", None, None),
    ("GET", "/api/projects?page=4611686018427387904", None, None),
    ("POST", "/login", None, {"email": "a@b.c", "password": 5}),
    ("POST", "/login", None, {"email": "a@b.c", "password": ["x"]}),
    ("POST", "/register", None, {"email": "new@b.c", "password": 123456789}),
    ("POST", "/set-password/abc", None, {"password": 123456789}),
    ("POST", "/organizer/assign", ORGANIZER, {"k": [1]}),
    ("POST", "/organizer/judges", ORGANIZER, {"email": "x@y.z", "tracks": [1]}),
    ("POST", "/organizer/event", ORGANIZER, {"submissions_close": "9999-12-31T23:59:59-23:59"}),
    ("POST", "/organizer/events", ORGANIZER, {"submissions_close": "0001-01-01T00:00:00+05:00"}),
]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with portal(tmp_path_factory.mktemp("regressions")) as c:
        yield c


@pytest.mark.parametrize("method,path,headers,body", MALFORMED, ids=[f"{m} {p} {b}" for m, p, _, b in MALFORMED])
def test_bug6_no_500_on_malformed_input(client, method, path, headers, body):
    r = client.request(method, path, json=body, headers=headers or {})
    assert r.status_code < 500


# BUG-8 -- lifecycle.md case 13 + edge "Fixture judges (no password) can be given one via the
# organizer's set-password link": the link is for accounts without a password, not a reset of
# someone else's existing password.
def test_bug8_judge_invite_cannot_reset_an_existing_password(tmp_path):
    with portal(tmp_path, demo=True) as c:
        # In demo mode jdg_26's user (jonas.vogel@example.org) has the password "dogfood-demo".
        assert c.post("/login", data={"email": "jonas.vogel@example.org",
                                      "password": "dogfood-demo"}).status_code == 303
        r = c.post("/organizer/judges", data={"email": "jonas.vogel@example.org"}, headers=ORGANIZER)
        loc = r.headers.get("location", "")
        if "link=" in loc:
            c.post(loc.split("link=", 1)[1], data={"password": "organizer-chosen-pw"})
        assert c.post("/login", data={"email": "jonas.vogel@example.org",
                                      "password": "organizer-chosen-pw"}).status_code == 401
        assert c.post("/login", data={"email": "jonas.vogel@example.org",
                                      "password": "dogfood-demo"}).status_code == 303


# BUG-7 -- lifecycle.md Outputs ("one audit_log row for every state-changing organizer action").
def test_bug7_failed_judge_invite_leaves_no_unaudited_judge(client):
    client.post("/organizer/judges", json={"email": "partial@example.org", "tracks": [1]}, headers=ORGANIZER)
    conn = client.app.state.conn
    judges = conn.execute("SELECT COUNT(*) FROM judges j JOIN users u ON u.id = j.user_id "
                          "WHERE u.email = 'partial@example.org'").fetchone()[0]
    audits = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'judge.invite' "
                          "AND detail LIKE '%partial@example.org%'").fetchone()[0]
    assert judges == audits
