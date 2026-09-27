"""Background webhook delivery with retries. Never runs on a request's path.

One thread, its own sqlite connection. Nothing is sent unless an organizer has
configured a webhook (offline rule).
"""

import json
import secrets
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import db, logs
from .core import webhooks
from .core.timeutil import format_utc, parse_utc

POLL_S = 1.0
TIMEOUT_S = 5
PARALLEL = 8          # sends in flight at once: one dead receiver cannot hold up everyone else's
KEEP_DAYS = 30        # finished deliveries (and their attempt logs) older than this are pruned


def enqueue(conn, event_id: str, event: str, data: dict, now: datetime) -> int:
    """Queue one delivery per matching webhook. Call inside the caller's transaction or after it."""
    hooks = [h for h in conn.execute("SELECT id, events FROM webhooks WHERE event_id = ?", (event_id,))
             if event in json.loads(h["events"])]
    stamp = format_utc(now)
    for h in hooks:
        did = "whd_" + secrets.token_hex(8)
        conn.execute("INSERT INTO webhook_deliveries (id, webhook_id, event, body, state, next_at, created_at) "
                     "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                     (did, h["id"], event, webhooks.body(did, event, stamp, data).decode("utf-8"), stamp, stamp))
    if hooks:
        logs.stage("webhooks", "enqueued", event_id=event_id, webhook_event=event, count=len(hooks))
    return len(hooks)


class Worker:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="webhook-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=TIMEOUT_S + 2)

    def _run(self) -> None:
        conn = db.connect(self.db_path)
        try:
            while not self._stop.wait(POLL_S):
                try:
                    self.tick(conn, datetime.now(timezone.utc))
                except Exception as e:  # the worker must survive anything; log and carry on
                    logs.stage("webhooks", "worker_error", error=f"{type(e).__name__}: {e}")
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
        finally:
            conn.close()

    def tick(self, conn, now: datetime) -> None:
        self._voting_closed(conn, now)
        self._housekeeping(conn, now)
        due = conn.execute("SELECT d.*, w.url, w.secret FROM webhook_deliveries d JOIN webhooks w "
                           "ON w.id = d.webhook_id WHERE d.state = 'pending' AND d.next_at <= ? "
                           "ORDER BY d.next_at LIMIT 20", (format_utc(now),)).fetchall()
        if not due:
            return
        # Network in parallel threads; every database write stays on this thread's connection.
        with ThreadPoolExecutor(max_workers=min(PARALLEL, len(due))) as pool:
            outcomes = list(pool.map(_send, due))
        for d, (status, error) in zip(due, outcomes):
            self._record(conn, d, now, status, error)
        self._prune(conn, now)

    _last_housekeeping = None

    def _housekeeping(self, conn, now: datetime) -> None:
        """Hourly: expired login sessions and set-password links would otherwise pile up forever."""
        if self._last_housekeeping and now - self._last_housekeeping < timedelta(hours=1):
            return
        self._last_housekeeping = now
        stamp = format_utc(now)
        with db.transaction(conn):
            sessions = conn.execute("DELETE FROM sessions WHERE expires_at IS NOT NULL AND expires_at <= ?",
                                    (stamp,)).rowcount
            links = conn.execute("DELETE FROM password_links WHERE expires_at <= ?", (stamp,)).rowcount
        if sessions or links:
            logs.stage("housekeeping", "pruned", sessions=sessions, password_links=links)

    def _prune(self, conn, now: datetime) -> None:
        cutoff = format_utc(now - timedelta(days=KEEP_DAYS))
        with db.transaction(conn):
            conn.execute("DELETE FROM webhook_deliveries WHERE state != 'pending' AND created_at < ?", (cutoff,))

    def _voting_closed(self, conn, now: datetime) -> None:
        for ev in conn.execute("SELECT id, voting_close FROM events WHERE voting_close IS NOT NULL "
                               "AND voting_closed_sent = 0").fetchall():
            if parse_utc(ev["voting_close"]) <= now:
                with db.transaction(conn):
                    if conn.execute("UPDATE events SET voting_closed_sent = 1 WHERE id = ? AND voting_closed_sent = 0",
                                    (ev["id"],)).rowcount:
                        enqueue(conn, ev["id"], "voting.closed", {"event": ev["id"]}, now)

    def _record(self, conn, d, now: datetime, status, error) -> None:
        attempt = d["attempts"] + 1
        ok = status is not None and 200 <= status < 300
        if ok:
            state, next_at = "delivered", now
        elif attempt <= len(webhooks.RETRY_DELAYS):
            state, next_at = "pending", now + timedelta(seconds=webhooks.RETRY_DELAYS[attempt - 1])
        else:
            state, next_at = "failed", now
        with db.transaction(conn):
            conn.execute("INSERT INTO delivery_attempts VALUES (?, ?, ?, ?, ?)",
                         (d["id"], attempt, format_utc(now), status, error))
            conn.execute("UPDATE webhook_deliveries SET state = ?, attempts = ?, last_status = ?, last_error = ?, "
                         "next_at = ? WHERE id = ?", (state, attempt, status, error, format_utc(next_at), d["id"]))
        logs.stage("webhooks", "delivery", delivery=d["id"], attempt=attempt, status=status, state=state, error=error)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a failed delivery: urllib would re-send the POST as a body-less GET (then
    log 'delivered'), and following it lets a receiver bounce the portal to internal hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _send(d) -> tuple[int | None, str | None]:
    """POST one delivery. Returns (http status or None, error text or None). No database access."""
    raw = d["body"].encode("utf-8")
    req = urllib.request.Request(d["url"], data=raw, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "dogfood-webhooks/1",
        "X-Dogfood-Event": d["event"], "X-Dogfood-Delivery": d["id"],
        "X-Dogfood-Signature": webhooks.signature(d["secret"], raw)})
    status, error = None, None
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:300]
    return status, error
