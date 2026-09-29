"""BUG-37 regression: re-saving the Event form with unchanged voting dates is not a move.

Source of truth (hand-derived, not from running the code):
- BUGLOG.md BUG-37: after a tally was shown, saving the organizer Event form to change
  anything (deadlines, prizes) returned 409 `voting_closed_final` because the form always
  re-sends the prefilled voting dates. The lock must check whether the window changed,
  not whether the fields were present.
- contracts/t3-public.md, edge cases: once any tally has been shown the voting window is
  final (409 `voting_closed_final`); re-saving the event form with the same voting dates is
  not a change, other fields (deadlines, prizes) stay editable.

Window setup and reveal follow tests/golden/test_t3_regressions.py (BUG-33): set a CLOSED
window through the JSON route, then GET /api/v1/votes/results shows the tally.
No mocks: the real app runs through conftest.portal().
"""

import html
import re

from conftest import ORGANIZER, portal

CLOSED = {"voting_open": "2026-01-01T00:00:00Z", "voting_close": "2026-02-01T00:00:00Z"}


def _close_and_reveal(c):
    r = c.post("/api/v1/event", json=CLOSED, headers=ORGANIZER)
    assert r.status_code == 303, r.text
    r = c.get("/api/v1/votes/results")
    assert r.status_code == 200, r.text   # a tally (possibly empty) has now been shown


def _event(c):
    r = c.get("/api/v1/events/evt_01/export.json", headers=ORGANIZER)
    assert r.status_code == 200, r.text
    return r.json()["event"]


def _form_values(page):
    """The prefilled inputs of the organizer Event form, exactly as a browser would re-send them."""
    m = re.search(r'<form method="post" action="/organizer/event\?event=evt_01">(.*?)</form>', page, re.S)
    assert m, "Event form not found on /organizer"
    values = {}
    for tag in re.findall(r"<input\b[^>]*>", m.group(1)):
        name = re.search(r'name="([^"]*)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            values[name.group(1)] = html.unescape(value.group(1)) if value else ""
    return values


# (1) form route: re-save the prefilled form with only prizes changed -> 303, prizes stored
def test_bug37_form_resave_same_window_new_prizes_is_303(tmp_path):
    with portal(tmp_path) as c:
        _close_and_reveal(c)
        page = c.get("/organizer", headers=ORGANIZER)
        assert page.status_code == 200
        form = _form_values(page.text)
        # the form carries the current window back unchanged (the trigger of BUG-37)
        assert form["voting_open"] == CLOSED["voting_open"]
        assert form["voting_close"] == CLOSED["voting_close"]
        form["prizes"] = "BUG37 grand prize"
        r = c.post("/organizer/event?event=evt_01", data=form, headers=ORGANIZER)
        assert r.status_code == 303, r.text
        ev = _event(c)
        assert ev["prizes"] == "BUG37 grand prize"
        assert (ev["voting_open"], ev["voting_close"]) == (CLOSED["voting_open"], CLOSED["voting_close"])
        assert 'value="BUG37 grand prize"' in c.get("/organizer", headers=ORGANIZER).text


# (1b) form route, minimal body: same dates + prizes only
def test_bug37_form_same_dates_plus_prizes_is_303(tmp_path):
    with portal(tmp_path) as c:
        _close_and_reveal(c)
        r = c.post("/organizer/event", data={**CLOSED, "prizes": "Form prize"}, headers=ORGANIZER)
        assert r.status_code == 303, r.text
        assert _event(c)["prizes"] == "Form prize"


# (2) JSON route: same dates + prizes and judging_close -> 303, both stored
def test_bug37_json_same_dates_plus_other_fields_is_303(tmp_path):
    with portal(tmp_path) as c:
        _close_and_reveal(c)
        body = {**CLOSED, "prizes": "JSON prize", "judging_close": "2099-01-01T00:00:00Z"}
        r = c.post("/api/v1/event", json=body, headers=ORGANIZER)
        assert r.status_code == 303, r.text
        ev = _event(c)
        assert ev["prizes"] == "JSON prize"
        assert ev["judging_close"] == "2099-01-01T00:00:00Z"
        assert (ev["voting_open"], ev["voting_close"]) == (CLOSED["voting_open"], CLOSED["voting_close"])


# (3) actually moving voting_close after a reveal is still 409, on both routes, and nothing is saved
def test_bug37_changed_voting_close_after_reveal_still_409_json(tmp_path):
    with portal(tmp_path) as c:
        _close_and_reveal(c)
        before = _event(c)["prizes"]
        body = {"voting_open": CLOSED["voting_open"], "voting_close": "2099-01-01T00:00:00Z",
                "prizes": "should not save"}
        r = c.post("/api/v1/event", json=body, headers=ORGANIZER)
        assert (r.status_code, r.json()["error"]) == (409, "voting_closed_final")
        ev = _event(c)
        assert ev["voting_close"] == CLOSED["voting_close"]
        assert ev["prizes"] == before


def test_bug37_changed_voting_close_after_reveal_still_409_form(tmp_path):
    with portal(tmp_path) as c:
        _close_and_reveal(c)
        form = _form_values(c.get("/organizer", headers=ORGANIZER).text)
        form["voting_close"] = "2026-02-02T00:00:00Z"   # moved by one day, still in the past
        form["prizes"] = "should not save"
        r = c.post("/organizer/event?event=evt_01", data=form, headers=ORGANIZER)
        assert r.status_code == 409
        ev = _event(c)
        assert ev["voting_close"] == CLOSED["voting_close"]
        assert ev["prizes"] != "should not save"
