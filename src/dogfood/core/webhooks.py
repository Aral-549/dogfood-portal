"""Webhook payloads and signatures. See contracts/t4-api-webhooks-bulk.md.

Pure helpers; delivery (network, retries) lives in dogfood.webhook_worker.
"""

import hashlib
import hmac
import json
from urllib.parse import urlsplit

EVENTS = ("project.submitted", "project.updated", "score.submitted", "results.published", "voting.closed")
RETRY_DELAYS = (10, 60, 300)  # seconds after attempts 1, 2, 3; attempt 4 failing marks it failed
MIN_SECRET = 16


def validate(url, secret, events) -> str | None:
    """Error text, or None if the webhook definition is acceptable."""
    if not isinstance(url, str) or len(url) > 2000:
        return "url must be a string"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "url must be http(s) with a host"
    if not isinstance(secret, str) or len(secret) < MIN_SECRET:
        return f"secret must be at least {MIN_SECRET} characters"
    if not isinstance(events, list) or not events or not all(isinstance(e, str) and e in EVENTS for e in events):
        return f"events must be a non-empty subset of {list(EVENTS)}"
    return None


def body(delivery_id: str, event: str, created_at: str, data: dict) -> bytes:
    return json.dumps({"id": delivery_id, "event": event, "created_at": created_at, "data": data},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def signature(secret: str, raw_body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
