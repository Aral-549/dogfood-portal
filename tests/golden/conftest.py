"""Shared helpers for the golden suite.

Rules for this directory (AGENTS.md section 3): hand-verified ground truth.
Add new cases; never regenerate or rewrite existing ones without human approval.

Every expected value in this suite is taken from a contract in contracts/, derived by
hand arithmetic shown in a comment, or counted directly in fixtures.json by code that
is independent of src/dogfood. No mocks or monkeypatching of the code under test:
HTTP tests run the real app through FastAPI's TestClient against a fresh temporary
data directory, configured only through environment variables.
"""

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES_PATH = REPO / "fixtures.json"

# Fixed anchor for relative dates: the fixture event's close, as stated in
# contracts/fixtures-import.md case 3 and _upstream/context.txt section 9.
CLOSE = datetime(2026, 3, 1, 18, 0, 0, tzinfo=timezone.utc)
ONE_SECOND = timedelta(seconds=1)
EVENT_ID = "evt_01"

# Demo bearer tokens as fixed in contracts/acceptance.md.
ORGANIZER = {"Authorization": "Bearer demo-organizer"}
JUDGE_A = {"Authorization": "Bearer demo-judge-a"}      # jdg_24
JUDGE_B = {"Authorization": "Bearer demo-judge-b"}      # jdg_26
PARTICIPANT = {"Authorization": "Bearer demo-participant"}  # priya1@example.org, tm_01


def load_fixture_json() -> dict:
    """Raw upstream file, read with the stdlib only (independent of the importer)."""
    with open(FIXTURES_PATH, "rb") as f:
        return json.load(f)


@contextmanager
def env(**values):
    """Set (value=str) or unset (value=None) environment variables, restoring afterwards."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def portal(data_dir: Path, demo: bool = True):
    """Boot the real app (lifespan runs boot(): schema, fixture import, demo sessions)."""
    from fastapi.testclient import TestClient
    from dogfood.app import app

    with env(DOGFOOD_DATA=str(data_dir), DOGFOOD_FIXTURES=str(FIXTURES_PATH),
             DOGFOOD_DEMO_SESSIONS="1" if demo else None):
        with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
            yield client
