"""Structured JSON logs, one line per stage boundary."""

import json
import logging
import sys
from datetime import datetime, timezone

_logger = logging.getLogger("dogfood")


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.handlers[:] = [handler]
    _logger.setLevel(level)
    _logger.propagate = False


def stage(stage_name: str, what: str, /, **fields) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
              "stage": stage_name, "event": what, **fields}
    _logger.info(json.dumps(record, default=str, sort_keys=False))


def redact(token: str | None) -> str | None:
    return None if token is None else token[:4] + "..."
