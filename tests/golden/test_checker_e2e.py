"""contracts/acceptance.md case 8, end to end: the official run.py (unmodified) against a
REAL uvicorn server in a subprocess, on a fresh temporary data dir.

No TestClient, no mocks: the server is started exactly as documented
(DOGFOOD_DATA=<dir> DOGFOOD_DEMO_SESSIONS=1 PYTHONPATH=src uvicorn dogfood.app:app),
run.py reads a temp copy of our .dogfood.toml whose only change is base_url, and the
assertions are made on run.py's stdout.

Also pinned here:
- contracts/lifecycle.md case 22: moving the default event's submissions_close into
  the future really makes the "closed event refuses submissions" check FAIL.
- contracts/acceptance.md edge case: restart on the same data dir, same verdict.
- README "for a real event": restarting with DOGFOOD_DEMO_SESSIONS unset revokes the
  published tokens (401 everywhere), so the checks needing a working token fail.
- Two checker runs in a row leave the gallery and the projects table unchanged.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import JUDGE_A, JUDGE_B, ORGANIZER, PARTICIPANT, REPO

UVICORN = REPO / ".venv" / "bin" / "uvicorn"
RUN_PY = REPO / "run.py"
TOML = REPO / ".dogfood.toml"
READY_TIMEOUT = 20.0
RUN_TIMEOUT = 60.0

# The seven labels in run.py's build_checks(), in order, with their tier.
CHECKS = [
    ("T1", "gallery is public"),
    ("T1", "project from fixtures shown"),
    ("T1", "closed event refuses submissions"),
    ("T2", "judge sees own scores"),
    ("T2", "judge cannot see peer scores"),
    ("T2", "participant blocked"),
    ("T2", "csv export works"),
]
PROBE_TITLE = "dogfood-late-submission-probe"

pytestmark = pytest.mark.skipif(not UVICORN.exists(), reason=".venv/bin/uvicorn not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def server(data_dir: Path, demo: bool = True):
    """Real uvicorn subprocess on a free port; always killed on exit. Yields base_url."""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("DOGFOOD_")}
    env.update(DOGFOOD_DATA=str(data_dir), PYTHONPATH="src")
    if demo:
        env["DOGFOOD_DEMO_SESSIONS"] = "1"
    log = open(data_dir.parent / f"{data_dir.name}-uvicorn-{port}.log", "wb")
    proc = subprocess.Popen([str(UVICORN), "dogfood.app:app", "--port", str(port)],
                            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + READY_TIMEOUT
        while True:
            if proc.poll() is not None:
                raise AssertionError(f"uvicorn exited with {proc.returncode} before ready; see {log.name}")
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=1) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError(f"/healthz not ready after {READY_TIMEOUT}s; see {log.name}")
            time.sleep(0.05)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        log.close()


def run_checker(base: str, tmp: Path) -> list[str]:
    """Temp copy of .dogfood.toml with only the base_url port changed (host stays localhost,
    as committed); run.py --fixtures fixtures.json from the repo root."""
    port = base.rsplit(":", 1)[1]
    text = TOML.read_text()
    assert text.count('base_url = "http://localhost:8080"') == 1
    cfg = tmp / "dogfood.toml"
    cfg.write_text(text.replace('base_url = "http://localhost:8080"', f'base_url = "http://localhost:{port}"'))
    out = subprocess.run([sys.executable, str(RUN_PY), str(cfg), "--fixtures", "fixtures.json"],
                         cwd=REPO, capture_output=True, text=True, timeout=RUN_TIMEOUT)
    assert out.returncode == 0, out.stderr  # run.py always exits 0; anything else is a crash
    return out.stdout.splitlines()


def verdicts(lines: list[str]) -> dict[str, str]:
    """label -> PASS/FAIL, parsed from run.py's '<tier>  <label> .... PASS' lines."""
    found = {}
    for tier, label in CHECKS:
        rows = [ln for ln in lines if ln.startswith(f"{tier}  {label} .")]
        assert len(rows) == 1, (label, lines)
        found[label] = rows[0].rsplit(" ", 1)[1]
    return found


def assert_all_verified(lines: list[str]) -> None:
    assert verdicts(lines) == {label: "PASS" for _, label in CHECKS}, "\n".join(lines)
    # T3/T4 are claimed and judged by hand (kickoff brief); run.py has no checks for them.
    assert [ln for ln in lines if ln.strip()][-2:] == ["claimed T1 T2 T3 T4, verified T1 T2",
                                                        "note: claimed but not verified: T3 T4"], "\n".join(lines)
    assert "fixtures: fixtures.json" in lines


def gallery_snapshot(base: str) -> list[dict]:
    """Every page of the public gallery API."""
    items, page = [], 1
    while True:
        with urllib.request.urlopen(f"{base}/api/projects?page={page}", timeout=5) as r:
            body = json.loads(r.read())
        items += body["items"]
        if not body["items"] or len(items) >= body["total"]:
            assert len(items) == body["total"]
            return items
        page += 1


def projects_rows(data_dir: Path) -> list[tuple]:
    """Read the stored projects table directly (independent of the app's queries)."""
    conn = sqlite3.connect(f"file:{data_dir / 'dogfood.db'}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT id, title, status FROM projects ORDER BY id").fetchall()
    finally:
        conn.close()


def test_case8_fresh_portal_verifies_t1_t2(tmp_path):
    with server(tmp_path / "data") as base:
        lines = run_checker(base, tmp_path)
    assert_all_verified(lines)


def test_lifecycle22_moving_deadline_into_future_fails_closed_event_check(tmp_path):
    with server(tmp_path / "data") as base:
        req = urllib.request.Request(
            base + "/organizer/event", method="POST",
            data=json.dumps({"submissions_close": "2099-01-01T00:00:00Z"}).encode(),
            headers={**ORGANIZER, "Content-Type": "application/json"})
        opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                                  {"redirect_request": lambda *a, **k: None}))
        try:
            opener.open(req, timeout=5)
            status = 200
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 303  # update_event redirects back to /organizer on success
        lines = run_checker(base, tmp_path)
    v = verdicts(lines)
    assert v["closed event refuses submissions"] == "FAIL", "\n".join(lines)
    assert [lab for lab, res in v.items() if res == "FAIL"] == ["closed event refuses submissions"]
    # T1 fails, so T2 cannot count either (run.py: a tier needs every tier below it).
    assert "claimed T1 T2 T3 T4, verified nothing" in lines
    assert "note: claimed but not verified: T1 T2 T3 T4" in lines


def test_restart_on_same_data_dir_still_verifies(tmp_path):
    data = tmp_path / "data"
    with server(data) as base:
        assert_all_verified(run_checker(base, tmp_path))
    before = projects_rows(data)
    with server(data) as base:
        lines = run_checker(base, tmp_path)
    assert_all_verified(lines)
    assert projects_rows(data) == before  # second boot did not re-import or duplicate


def _status(url: str, headers: dict, method: str = "GET", body: dict | None = None) -> int:
    req = urllib.request.Request(url, method=method, headers=dict(headers),
                                 data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_demo_mode_off_revokes_published_tokens(tmp_path):
    data = tmp_path / "data"
    with server(data, demo=True) as base:
        assert_all_verified(run_checker(base, tmp_path))
    with server(data, demo=False) as base:
        lines = run_checker(base, tmp_path)
        # Every published token is now unknown: 401 everywhere, including the submit probe.
        assert _status(base + "/api/judge/scores", JUDGE_A) == 401
        assert _status(base + "/api/export.csv", ORGANIZER) == 401
        assert _status(base + "/api/judges/jdg_24/scores", JUDGE_B) == 401
        assert _status(base + "/api/judge/scores", PARTICIPANT) == 401
        assert _status(base + "/api/projects", PARTICIPANT, "POST",
                       {"title": PROBE_TITLE, "summary": "probe"}) == 401
    v = verdicts(lines)
    # The checks that need a working token fail ...
    assert v["judge sees own scores"] == "FAIL", "\n".join(lines)
    assert v["csv export works"] == "FAIL", "\n".join(lines)
    # ... while every check that only wants a 4xx refusal still passes, now on a 401 for
    # an unknown token. Note: run.py's T1 "closed event" check passes here WITHOUT the
    # deadline being tested at all (any 4xx counts), so T1 stays verified.
    for label in ("gallery is public", "project from fixtures shown", "closed event refuses submissions",
                  "judge cannot see peer scores", "participant blocked"):
        assert v[label] == "PASS", (label, "\n".join(lines))
    assert "claimed T1 T2 T3 T4, verified T1" in lines
    assert "note: claimed but not verified: T2 T3 T4" in lines


def test_two_runs_leave_gallery_and_projects_unchanged(tmp_path):
    data = tmp_path / "data"
    with server(data) as base:
        gallery0 = gallery_snapshot(base)
        rows0 = projects_rows(data)
        first = run_checker(base, tmp_path)
        second = run_checker(base, tmp_path)
        gallery2 = gallery_snapshot(base)
        rows2 = projects_rows(data)
    assert_all_verified(first)
    assert first == second
    assert gallery0 and gallery2 == gallery0
    assert rows2 == rows0
    assert not any(PROBE_TITLE in (r[1] or "") for r in rows2)
