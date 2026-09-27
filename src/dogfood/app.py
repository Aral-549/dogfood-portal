"""HTTP layer. Every protected handler asks core.authz first and acts on the Decision."""

import csv
import io
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import boot, db, logs, services as svc
from .core import authz, csvexport, importer, records, webhooks
from .core.assignment import auto_assign
from .core.confidence import tiebreak_assign
from .core.public import ballot_order, normalize_email, tally, voting_state
from .core.ratelimit import SlidingWindow
from .webhook_worker import Worker, enqueue
from .core.authz import Actor, Decision
from .core.deadline import submissions_open
from .core.timeutil import format_utc, parse_utc

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SESSION_COOKIE = "session"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
MAX_WEIGHT = 1000.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    logs.setup(os.environ.get("DOGFOOD_LOG_LEVEL", "INFO"))
    # Nested lifespans (e.g. two test clients) must not clobber each other.
    previous = (getattr(app.state, "conn", None), getattr(app.state, "db_path", None))
    conn = app.state.conn = boot.boot()  # boot-time connection; requests open their own
    app.state.db_path = boot.db_path()
    worker = Worker(app.state.db_path)
    worker.start()
    app.state.limits = {  # contracts/t3-public.md cases 18-20; in memory, cleared by a restart
        "vote": SlidingWindow(10, 60), "comment": SlidingWindow(5, 60),
        "login_fail": SlidingWindow(5, 300), "register_ip": SlidingWindow(3, 3600),
    }
    yield
    worker.stop()
    conn.close()
    app.state.conn, app.state.db_path = previous


app = FastAPI(title="DOGFOOD portal", lifespan=lifespan, docs_url=None, redoc_url=None)


# --- request plumbing ------------------------------------------------------------

@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = uuid.uuid4().hex[:12]
    request.state.request_id = rid
    # One connection per request: sqlite connections are not safe to share across the
    # threadpool, and a request that fails mid-transaction must not poison the next one.
    conn = request.state.conn = db.connect(request.app.state.db_path)
    try:
        return await _handle(request, call_next, conn, rid)
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
            logs.stage("db", "rollback_open_transaction", request_id=rid, path=request.url.path)
        conn.close()


async def _handle(request: Request, call_next, conn: sqlite3.Connection, rid: str):
    now = svc.utcnow()
    token, source = authz.extract_token(request.headers.get("authorization"), request.cookies.get(SESSION_COOKIE))
    actor = svc.load_actor(conn, token, now)
    request.state.actor, request.state.token, request.state.now = actor, token, now
    request.state.token_source = source
    if source == "bearer" and request.cookies.get(SESSION_COOKIE):
        cookie_actor = svc.load_actor(conn, request.cookies.get(SESSION_COOKIE), now)
        if cookie_actor.user_id != actor.user_id:
            logs.stage("auth", "credential_conflict", request_id=rid, bearer_user=actor.user_id,
                       cookie_user=cookie_actor.user_id, used="bearer")
    logs.stage("auth", "actor", request_id=rid, method=request.method, path=request.url.path,
               token=logs.redact(token), source=source, user=actor.user_id)
    if request.method in UNSAFE and source == "cookie" and not _same_origin(request):
        logs.stage("auth", "csrf_block", request_id=rid, origin=request.headers.get("origin"))
        return JSONResponse({"error": "cross_origin"}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/embed/"):
        response.headers["Content-Security-Policy"] = "frame-ancestors *"
    else:  # clickjacking protection everywhere except the embeddable widget
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    logs.stage("http", "response", request_id=rid, status=response.status_code)
    return response


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # non-browser client; SameSite=Lax already blocks cross-site cookie POSTs
    return urlsplit(origin).netloc == request.headers.get("host")


def conn_of(request: Request) -> sqlite3.Connection:
    return request.state.conn


def actor_of(request: Request) -> Actor:
    return request.state.actor


def event_for(request: Request):
    conn = conn_of(request)
    ev = svc.get_event(conn, request.query_params.get("event") or svc.default_event_id(conn))
    return ev


def wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", "")


def deny(request: Request, decision: Decision, reason: str | None = None):
    reason = reason or {401: "unauthenticated", 403: "forbidden", 404: "not_found"}[decision.value]
    logs.stage("authz", "deny", request_id=request.state.request_id, path=request.url.path,
               user=actor_of(request).user_id, status=decision.value, reason=reason)
    if wants_json(request):
        return JSONResponse({"error": reason}, status_code=decision.value)
    if decision is Decision.UNAUTHENTICATED:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return render(request, "message.html", {"title": "Not allowed", "message": reason}, status=decision.value)


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    ev = event_for(request)
    base = {"request": request, "actor": actor_of(request), "event": ev,
            "csrf_ok": True, "now": format_utc(request.state.now)}
    return TEMPLATES.TemplateResponse(request, name, {**base, **(ctx or {})}, status_code=status)


class BadInput(Exception):
    """Request body that cannot be handled safely; answered with 422."""


@app.exception_handler(BadInput)
async def bad_input(request: Request, exc: BadInput):
    logs.stage("http", "bad_input", request_id=request.state.request_id, reason=str(exc))
    return JSONResponse({"error": "invalid", "detail": str(exc)}, status_code=422)


def _check_text(value, depth: int = 0) -> None:
    """Reject strings that cannot be stored as UTF-8 (lone surrogates) anywhere in the body."""
    if depth > 32:
        raise BadInput("body is nested too deeply")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise BadInput("body contains invalid unicode") from None
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_text(k, depth + 1)
            _check_text(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _check_text(v, depth + 1)


async def body_of(request: Request) -> dict:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        except RecursionError:
            raise BadInput("body is nested too deeply") from None
        _check_text(data)
        return data if isinstance(data, dict) else {}
    form = await request.form()
    data = {k: v for k, v in form.items()}
    _check_text(data)
    return data


def clean(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


# --- public ------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/projects", status_code=303)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


def _page(raw: str | None) -> int:
    try:
        return min(svc.MAX_PAGE, max(1, int(raw or 1)))
    except ValueError:
        return 1


def password_of(data: dict) -> str:
    value = data.get("password")
    return value if isinstance(value, str) else ""


@app.get("/projects", response_class=HTMLResponse)
def gallery_page(request: Request, q: str = "", track: str = "", page: str | None = None):
    ev = event_for(request)
    if ev is None:
        return render(request, "message.html", {"title": "No event", "message": "No event has been set up yet."})
    q = q[:200]
    items, total = svc.gallery(conn_of(request), ev["id"], q, track, _page(page))
    tracks = conn_of(request).execute("SELECT id, name FROM tracks WHERE event_id = ? ORDER BY id", (ev["id"],)).fetchall()
    return render(request, "gallery.html", {"items": items, "total": total, "q": q, "track": track,
                                            "tracks": tracks, "page": _page(page), "page_size": svc.PAGE_SIZE})


@app.get("/api/projects")
@app.get("/api/v1/projects")
def gallery_api(request: Request, q: str = "", track: str = "", page: str | None = None):
    ev = event_for(request)
    if ev is None:
        return JSONResponse({"error": "no_event"}, status_code=404)
    items, total = svc.gallery(conn_of(request), ev["id"], q[:200], track, _page(page))
    return {"event": ev["id"], "total": total, "page": _page(page), "items": items}


@app.get("/results", response_class=HTMLResponse)
def public_results(request: Request):
    ev = event_for(request)
    if ev is None or not ev["results_published"]:
        return render(request, "message.html", {"title": "Results", "message": "Results have not been published yet."},
                      status=404)
    res, meta = svc.results(conn_of(request), ev["id"])
    excluded = conn_of(request).execute(
        "SELECT COUNT(*) FROM judge_exclusions x JOIN judges j ON j.id = x.judge_id WHERE j.event_id = ? "
        "AND EXISTS (SELECT 1 FROM reviews r WHERE r.judge_id = x.judge_id)", (ev["id"],)).fetchone()[0]
    return render(request, "results.html", {"results": res, "meta": meta, "excluded_count": excluded})


# --- accounts --------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/me"):
    return render(request, "login.html", {"next": next if next.startswith("/") and not next.startswith("//") else "/me"})


@app.post("/login")
async def login(request: Request):
    data = await body_of(request)
    email, password = clean(data.get("email"), 254).lower(), password_of(data)
    nxt = data.get("next") or "/me"
    nxt = nxt if isinstance(nxt, str) and nxt.startswith("/") and not nxt.startswith("//") else "/me"
    conn = conn_of(request)
    fails = request.app.state.limits["login_fail"]
    blocked, retry = fails.blocked(email, _mono())
    if blocked:
        return _too_many(request, retry, "too many failed logins for this email")
    row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    if not await run_in_threadpool(svc.check_password, password, row["password_hash"] if row else None):
        fails.hit(email, _mono())
        logs.stage("auth", "login_failed", request_id=request.state.request_id)
        return render(request, "login.html", {"error": "Wrong email or password.", "next": nxt}, status=401)
    token = svc.create_session(conn, row["id"])
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=14 * 86400)
    return resp


@app.post("/logout")
def logout(request: Request):
    cookie_token = request.cookies.get(SESSION_COOKIE)
    if cookie_token:
        svc.delete_session(conn_of(request), cookie_token)
    resp = RedirectResponse("/projects", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return render(request, "register.html")


@app.post("/register")
async def register(request: Request):
    data = await body_of(request)
    email, name, password = clean(data.get("email"), 254).lower(), clean(data.get("name"), 100), password_of(data)
    if "@" not in email or len(password) < 8:
        return render(request, "register.html", {"error": "Valid email and a password of 8+ characters required."},
                      status=422)
    conn = conn_of(request)
    uid = "usr_" + secrets.token_hex(6)
    try:
        conn.execute("INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                     (uid, email, name, await run_in_threadpool(svc.hash_password, password),
                      format_utc(request.state.now)))
    except sqlite3.IntegrityError:
        return render(request, "register.html", {"error": "That email already has an account."}, status=409)
    _registration_flags(request, conn, uid, email)
    token = svc.create_session(conn, uid)
    resp = RedirectResponse("/me", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=14 * 86400)
    return resp


def _mono() -> float:
    return time.monotonic()


def _too_many(request: Request, retry: float, reason: str):
    logs.stage("abuse", "rate_limited", request_id=request.state.request_id, path=request.url.path, reason=reason)
    return JSONResponse({"error": "rate_limited", "detail": reason}, status_code=429,
                        headers={"Retry-After": str(max(1, int(retry + 0.999)))})


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _registration_flags(request: Request, conn, uid: str, email: str) -> None:
    """Flag, never block: shared IPs (campus NAT) and plus-addresses are normal."""
    norm = normalize_email(email)
    twins = [r["id"] for r in conn.execute("SELECT id, email FROM users WHERE id != ?", (uid,))
             if normalize_email(r["email"]) == norm]
    ip = _client_ip(request)
    by_ip = request.app.state.limits["register_ip"]
    ip_ok, _ = by_ip.hit(ip, _mono())
    flags = []
    if twins:
        flags.append(("duplicate_email", {"matches": twins, "normalized": norm}))
    if not ip_ok:
        flags.append(("many_accounts_one_ip", {"ip": ip}))
    for kind, detail in flags:
        with db.transaction(conn):
            conn.execute("INSERT INTO abuse_flags (user_id, kind, detail, at) VALUES (?, ?, ?, ?)",
                         (uid, kind, json.dumps(detail), format_utc(request.state.now)))
            svc.audit(conn, uid, None, "abuse.flag", uid, kind=kind, **detail)


@app.get("/set-password/{token}", response_class=HTMLResponse)
def set_password_page(request: Request, token: str):
    return render(request, "set_password.html", {"token": token})


@app.post("/set-password/{token}")
async def set_password(request: Request, token: str):
    password = password_of(await body_of(request))
    if len(password) < 8:
        return render(request, "set_password.html", {"token": token, "error": "8+ characters."}, status=422)
    uid = await run_in_threadpool(svc.consume_password_link, conn_of(request), token, password)
    if uid is None:
        return render(request, "message.html", {"title": "Link expired", "message": "This link is not valid."},
                      status=404)
    return RedirectResponse("/login", status_code=303)


@app.get("/me", response_class=HTMLResponse)
def me(request: Request):
    actor = actor_of(request)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    conn, ev = conn_of(request), event_for(request)
    user = conn.execute("SELECT email, name FROM users WHERE id = ?", (actor.user_id,)).fetchone()
    team = projects = None
    if ev and actor.team_id(ev["id"]):
        team = conn.execute("SELECT * FROM teams WHERE id = ?", (actor.team_id(ev["id"]),)).fetchone()
        projects = conn.execute("SELECT * FROM projects WHERE team_id = ? ORDER BY id", (team["id"],)).fetchall()
    is_open = bool(ev) and submissions_open(svc.event_close(ev), request.state.now)
    my_records = conn.execute("SELECT id, kind, event_id FROM records WHERE user_id = ? ORDER BY issued_at",
                              (actor.user_id,)).fetchall()
    return render(request, "me.html", {"user": user, "team": team, "projects": projects, "is_open": is_open,
                                       "my_records": my_records})


# --- teams ------------------------------------------------------------------------------

@app.post("/teams")
@app.post("/api/v1/teams")
async def create_team(request: Request):
    conn, actor, ev = conn_of(request), actor_of(request), event_for(request)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    if ev is None or not submissions_open(svc.event_close(ev), request.state.now):
        return deny(request, Decision.FORBIDDEN, "submissions_closed")
    if actor.team_id(ev["id"]):
        return render(request, "message.html", {"title": "Already in a team", "message": "Leave it first."}, status=409)
    name = clean((await body_of(request)).get("name"), 80) or "Unnamed team"
    tid = "tm_" + secrets.token_hex(5)
    conn.execute("INSERT INTO teams (id, event_id, name, invite_code) VALUES (?, ?, ?, ?)",
                 (tid, ev["id"], name, secrets.token_urlsafe(12)))
    conn.execute("INSERT INTO team_members VALUES (?, ?, ?)", (tid, actor.user_id, ev["id"]))
    svc.audit(conn, actor, ev["id"], "team.create", tid, name=name)
    return RedirectResponse("/me", status_code=303)


@app.get("/join/{code}", response_class=HTMLResponse)
def join_page(request: Request, code: str):
    team = conn_of(request).execute("SELECT * FROM teams WHERE invite_code = ?", (code,)).fetchone()
    if team is None:
        return render(request, "message.html", {"title": "Invite", "message": "Unknown invite link."}, status=404)
    return render(request, "join.html", {"team": team, "code": code})


@app.post("/join/{code}")
@app.post("/api/v1/join/{code}")
def join(request: Request, code: str):
    conn, actor = conn_of(request), actor_of(request)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    team = conn.execute("SELECT * FROM teams WHERE invite_code = ?", (code,)).fetchone()
    if team is None:
        return render(request, "message.html", {"title": "Invite", "message": "Unknown invite link."}, status=404)
    ev = svc.get_event(conn, team["event_id"])
    if not submissions_open(svc.event_close(ev), request.state.now):
        return deny(request, Decision.FORBIDDEN, "submissions_closed")
    if actor.team_id(ev["id"]):
        return render(request, "message.html", {"title": "Invite", "message": "You are already in a team."}, status=409)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if svc.team_size(conn, team["id"]) >= svc.MAX_TEAM_SIZE:
            conn.execute("ROLLBACK")
            return render(request, "message.html", {"title": "Invite", "message": "Team is full."}, status=409)
        conn.execute("INSERT INTO team_members VALUES (?, ?, ?)", (team["id"], actor.user_id, ev["id"]))
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return render(request, "message.html", {"title": "Invite", "message": "You are already in a team."}, status=409)
    svc.audit(conn, actor, ev["id"], "team.join", team["id"])
    return RedirectResponse("/me", status_code=303)


# --- submissions ------------------------------------------------------------------------

@app.get("/projects/new", response_class=HTMLResponse)
def new_project_page(request: Request):
    ev = event_for(request)
    if ev is None:
        return render(request, "message.html", {"title": "No event", "message": "No event has been set up yet."},
                      status=404)
    decision, reason = authz.submit_project(actor_of(request), ev["id"],
                                            submissions_open(svc.event_close(ev), request.state.now))
    if not decision.allowed:
        return deny(request, decision, reason)
    tracks = conn_of(request).execute("SELECT id, name FROM tracks WHERE event_id = ?", (ev["id"],)).fetchall() if ev else []
    return render(request, "project_form.html", {"p": None, "tracks": tracks})


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: str):
    conn, actor = conn_of(request), actor_of(request)
    p = conn.execute("SELECT p.*, t.name AS team, tr.name AS track FROM projects p JOIN teams t ON t.id = p.team_id "
                     "LEFT JOIN tracks tr ON tr.id = p.track_id WHERE p.id = ?", (project_id,)).fetchone()
    own = p is not None and actor.team_id(p["event_id"]) == p["team_id"]
    if p is None or (p["status"] != "submitted" and not own):
        return render(request, "message.html", {"title": "Not found", "message": "No such project."}, status=404)
    ev = svc.get_event(conn, p["event_id"])
    can_edit = own and submissions_open(svc.event_close(ev), request.state.now)
    comments = conn.execute("SELECT c.id, c.body, c.created_at, c.user_id, COALESCE(NULLIF(u.name, ''), 'A participant') "
                            "AS author FROM comments c JOIN users u ON u.id = c.user_id "
                            "WHERE c.project_id = ? AND c.deleted_at IS NULL ORDER BY c.id", (project_id,)).fetchall()
    return render(request, "project.html", {"p": p, "can_edit": can_edit, "comments": comments,
                                            "is_organizer": actor.is_organizer(p["event_id"])})


def _validate_project(data: dict, conn, event_id: str) -> tuple[dict | None, str | None]:
    title = clean(data.get("title"), 1000)
    if not title or len(title) > 200:
        return None, "title is required, at most 200 characters"
    track = clean(data.get("track_id"), 64) or None
    if track and not conn.execute("SELECT 1 FROM tracks WHERE id = ? AND event_id = ?", (track, event_id)).fetchone():
        return None, "unknown track"
    repo = clean(data.get("repo_url"), 500)
    if repo and not repo.startswith(("https://", "http://")):
        return None, "repo_url must be an http(s) URL"
    draft = data.get("draft") in (True, "1", "true", "on")
    return {"title": title, "summary": clean(data.get("summary"), 2000), "repo_url": repo,
            "track_id": track, "status": "draft" if draft else "submitted"}, None


@app.post("/projects/new")
@app.post("/api/projects")
@app.post("/api/v1/projects")
async def create_project(request: Request):
    conn, actor, ev = conn_of(request), actor_of(request), event_for(request)
    if ev is None:
        return JSONResponse({"error": "no_event"}, status_code=404)
    is_open = submissions_open(svc.event_close(ev), request.state.now)
    decision, reason = authz.submit_project(actor, ev["id"], is_open)
    if not decision.allowed:
        return deny(request, decision, reason)
    fields, error = _validate_project(await body_of(request), conn, ev["id"])
    if error:
        return JSONResponse({"error": "invalid", "detail": error}, status_code=422) if wants_json(request) else \
            render(request, "project_form.html", {"p": None, "error": error, "tracks": []}, status=422)
    pid = "prj_" + secrets.token_hex(5)
    stamp = format_utc(request.state.now)
    conn.execute(
        "INSERT INTO projects (id, event_id, team_id, track_id, title, summary, repo_url, status, submitted_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, ev["id"], actor.team_id(ev["id"]), fields["track_id"], fields["title"], fields["summary"],
         fields["repo_url"], fields["status"], stamp if fields["status"] == "submitted" else None, stamp, stamp))
    svc.audit(conn, actor, ev["id"], "project.create", pid, status=fields["status"])
    if fields["status"] == "submitted":
        enqueue(conn, ev["id"], "project.submitted", {"project": pid, "title": fields["title"]}, request.state.now)
    if wants_json(request):
        return JSONResponse({"id": pid, **fields}, status_code=201)
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.get("/projects/{project_id}/edit", response_class=HTMLResponse)
def edit_project_page(request: Request, project_id: str):
    conn = conn_of(request)
    p = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if p is None:
        return render(request, "message.html", {"title": "Not found", "message": "No such project."}, status=404)
    ev = svc.get_event(conn, p["event_id"])
    decision, reason = authz.edit_project(actor_of(request), ev["id"], p["team_id"],
                                          submissions_open(svc.event_close(ev), request.state.now))
    if not decision.allowed:
        return deny(request, decision, reason)
    tracks = conn.execute("SELECT id, name FROM tracks WHERE event_id = ?", (ev["id"],)).fetchall()
    return render(request, "project_form.html", {"p": p, "tracks": tracks})


@app.post("/projects/{project_id}/edit")
@app.put("/api/projects/{project_id}")
@app.put("/api/v1/projects/{project_id}")
async def edit_project(request: Request, project_id: str):
    conn, actor = conn_of(request), actor_of(request)
    p = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if p is None:
        return deny(request, Decision.NOT_FOUND)
    ev = svc.get_event(conn, p["event_id"])
    decision, reason = authz.edit_project(actor, ev["id"], p["team_id"],
                                          submissions_open(svc.event_close(ev), request.state.now))
    if not decision.allowed:
        return deny(request, decision, reason)
    fields, error = _validate_project(await body_of(request), conn, ev["id"])
    if error:
        return JSONResponse({"error": "invalid", "detail": error}, status_code=422)
    stamp = format_utc(request.state.now)
    submitted_at = p["submitted_at"] or (stamp if fields["status"] == "submitted" else None)
    conn.execute("UPDATE projects SET title = ?, summary = ?, repo_url = ?, track_id = ?, status = ?, "
                 "submitted_at = ?, updated_at = ? WHERE id = ?",
                 (fields["title"], fields["summary"], fields["repo_url"], fields["track_id"], fields["status"],
                  submitted_at, stamp, project_id))
    svc.audit(conn, actor, ev["id"], "project.edit", project_id, status=fields["status"])
    became_submitted = p["status"] != "submitted" and fields["status"] == "submitted"
    enqueue(conn, ev["id"], "project.submitted" if became_submitted else "project.updated",
            {"project": project_id, "title": fields["title"], "status": fields["status"]}, request.state.now)
    if wants_json(request):
        return {"id": project_id, **fields}
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# --- judging -------------------------------------------------------------------------------

@app.get("/api/judge/scores")
@app.get("/api/v1/judge/scores")
def my_scores(request: Request):
    actor, ev = actor_of(request), event_for(request)
    if ev is None:
        return JSONResponse({"error": "no_event"}, status_code=404)
    decision = authz.read_own_scores(actor, ev["id"])
    if not decision.allowed:
        return deny(request, decision)
    judge_id = actor.judge_id(ev["id"])
    return {"judge": judge_id, "scores": svc.judge_scores(conn_of(request), judge_id)}


@app.get("/api/judges/{judge_id}/scores")
@app.get("/api/v1/judges/{judge_id}/scores")
def judge_scores_by_id(request: Request, judge_id: str):
    conn, actor = conn_of(request), actor_of(request)
    row = conn.execute("SELECT event_id FROM judges WHERE id = ?", (judge_id,)).fetchone()
    event_id = row["event_id"] if row else (svc.default_event_id(conn) or "")
    decision = authz.read_judge_scores(actor, event_id, judge_id, row is not None)
    if not decision.allowed:
        return deny(request, decision)
    return {"judge": judge_id, "scores": svc.judge_scores(conn, judge_id)}


@app.get("/judge", response_class=HTMLResponse)
def judge_home(request: Request):
    conn, actor, ev = conn_of(request), actor_of(request), event_for(request)
    if ev is None:
        return deny(request, Decision.NOT_FOUND)
    decision = authz.read_own_scores(actor, ev["id"])
    if not decision.allowed:
        return deny(request, decision)
    jid = actor.judge_id(ev["id"])
    assigned = conn.execute(
        "SELECT p.id, p.title, p.summary, p.repo_url, r.id AS review_id, r.comment FROM assignments a "
        "JOIN projects p ON p.id = a.project_id LEFT JOIN reviews r ON r.judge_id = a.judge_id AND r.project_id = p.id "
        "WHERE a.judge_id = ? ORDER BY p.id", (jid,)).fetchall()
    mine = {s["project"]: s["criteria"] for s in svc.judge_scores(conn, jid)}
    return render(request, "judge.html", {"assigned": assigned, "mine": mine, "rubric": svc.rubric(conn, ev["id"]),
                                          "judging_open": svc.judging_open(ev, request.state.now)})


@app.post("/judge/projects/{project_id}")
@app.post("/api/judge/scores/{project_id}")
@app.post("/api/v1/judge/scores/{project_id}")
async def submit_score(request: Request, project_id: str):
    conn, actor = conn_of(request), actor_of(request)
    p = conn.execute("SELECT event_id, team_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if p is None:
        return deny(request, Decision.NOT_FOUND)
    ev = svc.get_event(conn, p["event_id"])
    jid = actor.judge_id(ev["id"])
    assigned = bool(jid) and conn.execute("SELECT 1 FROM assignments WHERE judge_id = ? AND project_id = ?",
                                          (jid, project_id)).fetchone() is not None
    decision, reason = authz.score_project(actor, ev["id"], assigned, svc.judging_open(ev, request.state.now),
                                           project_team_id=p["team_id"])
    if not decision.allowed:
        return deny(request, decision, reason)
    data = await body_of(request)
    crit_in = data.get("criteria") if isinstance(data.get("criteria"), dict) else data
    values = {}
    for name in svc.rubric(conn, ev["id"]):
        try:
            v = int(str(crit_in.get(name, "")).strip())
        except ValueError:
            v = None
        if v is None or not 1 <= v <= 5:
            return JSONResponse({"error": "invalid", "detail": f"{name} must be an integer 1..5"}, status_code=422)
        values[name] = v
    comment = clean(data.get("comment"), 4000)
    stamp = format_utc(request.state.now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        rid = conn.execute(
            "INSERT INTO reviews (judge_id, project_id, comment, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(judge_id, project_id) DO UPDATE SET comment = excluded.comment, updated_at = excluded.updated_at "
            "RETURNING id", (jid, project_id, comment, stamp)).fetchone()["id"]
        conn.execute("DELETE FROM review_scores WHERE review_id = ?", (rid,))
        conn.executemany("INSERT INTO review_scores VALUES (?, ?, ?)", [(rid, k, v) for k, v in values.items()])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    svc.audit(conn, actor, ev["id"], "score.submit", project_id, judge=jid, criteria=values)
    # Never the values: webhooks must not become a side door around peer-score isolation.
    enqueue(conn, ev["id"], "score.submitted", {"judge": jid, "project": project_id}, request.state.now)
    if wants_json(request):
        return {"judge": jid, "project": project_id, "criteria": values, "comment": comment}
    return RedirectResponse("/judge", status_code=303)


# --- organizer ------------------------------------------------------------------------------

def _organizer_guard(request: Request):
    ev = event_for(request)
    if ev is None:
        return None, deny(request, Decision.NOT_FOUND)
    decision = authz.manage_event(actor_of(request), ev["id"])
    return ev, (None if decision.allowed else deny(request, decision))


@app.get("/api/export.csv")
@app.get("/api/v1/export.csv")
def export_csv(request: Request):
    ev = event_for(request)
    if ev is None:
        return JSONResponse({"error": "no_event"}, status_code=404)
    decision = authz.export_results(actor_of(request), ev["id"])
    if not decision.allowed:
        return deny(request, decision)
    res, meta = svc.results(conn_of(request), ev["id"])
    body = csvexport.results_csv(res, meta)
    svc.audit(conn_of(request), actor_of(request), ev["id"], "results.export", rows=len(res))
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="results-{ev["id"]}.csv"'})


@app.get("/organizer", response_class=HTMLResponse)
def organizer_home(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    conn = conn_of(request)
    k = _prize_places(request.query_params.get("k"))
    a = svc.analysis(conn, ev["id"], k)
    res, meta = a["results"], a["meta"]
    duplicates = [(pid, m["superseded_by"]) for pid, m in meta.items() if m["superseded_by"]]
    audit_rows = conn.execute("SELECT * FROM audit_log WHERE event_id = ? OR event_id IS NULL ORDER BY id DESC LIMIT 50",
                              (ev["id"],)).fetchall()
    judges = conn.execute("SELECT j.id, u.email FROM judges j JOIN users u ON u.id = j.user_id WHERE j.event_id = ?",
                          (ev["id"],)).fetchall()
    tracks = conn.execute("SELECT id, name FROM tracks WHERE event_id = ? ORDER BY id", (ev["id"],)).fetchall()
    return render(request, "organizer.html", {
        "results": res, "meta": meta, "progress": svc.progress(conn, ev["id"]), "duplicates": duplicates,
        "rubric": svc.rubric(conn, ev["id"]), "audit": audit_rows, "judges": judges, "tracks": tracks,
        "links": request.query_params.get("link"), "k": k, "confidence": a["confidence"],
        "is_checker_event": ev["id"] == svc.default_event_id(conn),
        "voting": {"state": _vstate(ev, request.state.now),
                   "total": conn.execute("SELECT COUNT(*) FROM votes WHERE event_id = ?", (ev["id"],)).fetchone()[0]},
        "abuse": conn.execute("SELECT f.*, u.email FROM abuse_flags f JOIN users u ON u.id = f.user_id "
                              "ORDER BY f.id DESC LIMIT 100").fetchall(),
        "records_count": conn.execute("SELECT COUNT(*) FROM records WHERE event_id = ?", (ev["id"],)).fetchone()[0],
        "hooks": [dict(h) | {"events": json.loads(h["events"])} for h in conn.execute(
            "SELECT id, url, events FROM webhooks WHERE event_id = ?", (ev["id"],))],
        "voided": {r["user_id"]: r["reason"] for r in conn.execute(
            "SELECT user_id, reason FROM voided_voters WHERE event_id = ?", (ev["id"],))},
        "agreement": a["agreement"], "favoritism": a["favoritism"], "excluded": a["excluded"]})


def _prize_places(raw) -> int:
    try:
        return max(1, min(50, int(raw or 3)))
    except (TypeError, ValueError, OverflowError):
        return 3


@app.get("/api/confidence.csv")
@app.get("/api/v1/confidence.csv")
def confidence_csv(request: Request):
    ev = event_for(request)
    if ev is None:
        return JSONResponse({"error": "no_event"}, status_code=404)
    decision = authz.export_results(actor_of(request), ev["id"])
    if not decision.allowed:
        return deny(request, decision)
    k = _prize_places(request.query_params.get("k"))
    a = svc.analysis(conn_of(request), ev["id"], k)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["rank", "project_id", "title", "p_top_k", "close_call", "unreviewed"])
    for r in a["results"]:
        c = a["confidence"].projects[r.project]
        w.writerow([r.rank, r.project, csvexport.safe_cell(a["meta"][r.project]["title"]), f"{c.p_top_k:.3f}",
                    "true" if c.close_call else "false", "true" if c.unreviewed else "false"])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="confidence-{ev["id"]}-top{k}.csv"'})


@app.get("/api/judge-agreement")
@app.get("/api/v1/judge-agreement")
def judge_agreement_api(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    a = svc.analysis(conn_of(request), ev["id"], _prize_places(request.query_params.get("k")))
    return {"event": ev["id"],
            "judges": [vars(x) | {"excluded": x.judge in a["excluded"]} for x in a["agreement"]],
            "favoritism": [vars(f) for f in a["favoritism"]],
            "excluded": list(a["excluded"].values())}


@app.post("/organizer/tiebreak")
@app.post("/api/v1/assignments/tiebreak")
async def run_tiebreak(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    conn = conn_of(request)
    k = _prize_places((await body_of(request)).get("k"))
    a = svc.analysis(conn, ev["id"], k)
    close = [(p, c.p_top_k) for p, c in a["confidence"].projects.items() if c.close_call] if a["confidence"] else []
    judge_tracks: dict[str, set[str]] = {r["id"]: set() for r in
                                         conn.execute("SELECT id FROM judges WHERE event_id = ?", (ev["id"],))}
    for r in conn.execute("SELECT jt.judge_id, jt.track_id FROM judge_tracks jt JOIN judges j ON j.id = jt.judge_id "
                          "WHERE j.event_id = ?", (ev["id"],)):
        judge_tracks[r["judge_id"]].add(r["track_id"])
    for jid in a["excluded"]:
        judge_tracks.pop(jid, None)  # never send an excluded judge to settle a close call
    existing = [(r["judge_id"], r["project_id"]) for r in conn.execute(
        "SELECT a.judge_id, a.project_id FROM assignments a JOIN judges j ON j.id = a.judge_id WHERE j.event_id = ?",
        (ev["id"],))]
    reviewed_by: dict[str, set[str]] = {}
    for r in conn.execute("SELECT r.judge_id, r.project_id FROM reviews r JOIN judges j ON j.id = r.judge_id "
                          "WHERE j.event_id = ?", (ev["id"],)):
        reviewed_by.setdefault(r["judge_id"], set()).add(r["project_id"])
    conflicts = {(r["jid"], r["pid"]) for r in conn.execute(
        "SELECT j.id AS jid, p.id AS pid FROM judges j JOIN team_members m ON m.user_id = j.user_id "
        "JOIN projects p ON p.team_id = m.team_id WHERE j.event_id = ?", (ev["id"],))}
    done = {r["project_id"] for r in conn.execute(
        "SELECT t.project_id FROM tiebreak_assignments t JOIN projects p ON p.id = t.project_id WHERE p.event_id = ?",
        (ev["id"],))}
    new, stuck = tiebreak_assign(close, {p: m["track_id"] for p, m in a["meta"].items()}, judge_tracks, existing,
                                 reviewed_by, conflicts, done)
    stamp = format_utc(request.state.now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany("INSERT OR IGNORE INTO assignments VALUES (?, ?)", new)
        conn.executemany("INSERT OR IGNORE INTO tiebreak_assignments VALUES (?, ?, ?)", [(j, p, stamp) for j, p in new])
        svc.audit(conn, actor_of(request), ev["id"], "assignment.tiebreak", ev["id"], k=k, added=new,
                  no_candidate=stuck)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return RedirectResponse(f"/organizer?event={ev['id']}&k={k}", status_code=303)


@app.post("/organizer/judges/{judge_id}/exclude")
@app.post("/organizer/judges/{judge_id}/include")
@app.post("/api/v1/judges/{judge_id}/exclude")
@app.post("/api/v1/judges/{judge_id}/include")
async def set_judge_exclusion(request: Request, judge_id: str):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    conn = conn_of(request)
    if not conn.execute("SELECT 1 FROM judges WHERE id = ? AND event_id = ?", (judge_id, ev["id"])).fetchone():
        return deny(request, Decision.NOT_FOUND)
    if request.url.path.endswith("/exclude"):
        reason = clean((await body_of(request)).get("reason"), 1000)
        if not reason:
            return JSONResponse({"error": "invalid", "detail": "a reason is required"}, status_code=422)
        with db.transaction(conn):
            conn.execute("INSERT INTO judge_exclusions VALUES (?, ?, ?, ?) ON CONFLICT(judge_id) DO UPDATE SET "
                         "reason = excluded.reason, excluded_by = excluded.excluded_by, at = excluded.at",
                         (judge_id, reason, actor_of(request).user_id, format_utc(request.state.now)))
            svc.audit(conn, actor_of(request), ev["id"], "judge.exclude", judge_id, reason=reason)
    else:
        with db.transaction(conn):
            removed = conn.execute("DELETE FROM judge_exclusions WHERE judge_id = ?", (judge_id,)).rowcount
            if removed:
                svc.audit(conn, actor_of(request), ev["id"], "judge.include", judge_id)
    return RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)


@app.post("/organizer/event")
@app.post("/api/v1/event")
async def update_event(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    data, conn = await body_of(request), conn_of(request)
    changes = {}
    try:
        for key in ("submissions_close", "judging_close", "voting_open", "voting_close"):
            raw = clean(data.get(key), 40)
            if raw:
                changes[key] = format_utc(parse_utc(raw))
        if "votes_per_voter" in data and str(data["votes_per_voter"]).strip():
            vpv = int(str(data["votes_per_voter"]).strip())
            if not 1 <= vpv <= 100:
                raise ValueError("votes_per_voter must be between 1 and 100")
            changes["votes_per_voter"] = vpv
    except (ValueError, OverflowError) as e:
        return JSONResponse({"error": "invalid", "detail": str(e)}, status_code=422)
    v_open = changes.get("voting_open", ev["voting_open"])
    v_close = changes.get("voting_close", ev["voting_close"])
    if bool(v_open) != bool(v_close) and ("voting_open" in changes or "voting_close" in changes):
        return JSONResponse({"error": "invalid", "detail": "set both voting_open and voting_close"}, status_code=422)
    if v_open and v_close and parse_utc(v_close) <= parse_utc(v_open):
        return JSONResponse({"error": "invalid", "detail": "voting_close must be after voting_open"}, status_code=422)
    for key in ("name", "prizes"):
        if isinstance(data.get(key), str):
            changes[key] = clean(data[key], 2000)
    with db.transaction(conn):
        for key, value in changes.items():
            conn.execute(f"UPDATE events SET {key} = ? WHERE id = ?", (value, ev["id"]))  # key from fixed tuple above
        svc.audit(conn, actor_of(request), ev["id"], "event.update", ev["id"],
                  before={k: ev[k] for k in changes}, after=changes)
    return RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)


@app.post("/organizer/events")
@app.post("/api/v1/events")
async def create_event(request: Request):
    actor = actor_of(request)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    if not actor.is_admin:
        return deny(request, Decision.FORBIDDEN)
    data, conn = await body_of(request), conn_of(request)
    try:
        close = format_utc(parse_utc(clean(data.get("submissions_close"), 40)))
        jclose = clean(data.get("judging_close"), 40)
        jclose = format_utc(parse_utc(jclose)) if jclose else None
    except ValueError as e:
        return JSONResponse({"error": "invalid", "detail": str(e)}, status_code=422)
    name = clean(data.get("name"), 200) or "New event"
    eid = "evt_" + secrets.token_hex(4)
    with db.transaction(conn):
        conn.execute("INSERT INTO events (id, name, submissions_close, judging_close, prizes, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (eid, name, close, jclose, clean(data.get("prizes"), 2000),
                                                    format_utc(request.state.now)))
        conn.execute("INSERT INTO organizers VALUES (?, ?)", (eid, actor.user_id))
        for i, tname in enumerate(t.strip() for t in clean(data.get("tracks"), 2000).split(",") if t.strip()):
            conn.execute("INSERT INTO tracks VALUES (?, ?, ?)", (f"{eid}_trk_{i + 1:02d}", eid, tname[:100]))
        for pos, crit in enumerate(("functionality", "quality", "innovation")):
            conn.execute("INSERT INTO criteria VALUES (?, ?, 1, ?)", (eid, crit, pos))
        svc.audit(conn, actor, eid, "event.create", eid, name=name)
    return RedirectResponse(f"/organizer?event={eid}", status_code=303)


@app.post("/organizer/rubric")
@app.post("/api/v1/rubric")
async def update_rubric(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    data, conn = await body_of(request), conn_of(request)
    current = svc.rubric(conn, ev["id"])
    new = {}
    for name in current:
        try:
            w = float(data.get(f"w_{name}", current[name]))
        except (TypeError, ValueError):
            w = -1
        if not 0 < w <= MAX_WEIGHT:
            return JSONResponse({"error": "invalid", "detail": f"weight for {name} must be in (0, {MAX_WEIGHT:g}]"},
                                status_code=422)
        new[name] = w
    with db.transaction(conn):
        for name, w in new.items():
            conn.execute("UPDATE criteria SET weight = ? WHERE event_id = ? AND name = ?", (w, ev["id"], name))
        svc.audit(conn, actor_of(request), ev["id"], "rubric.update", ev["id"], before=current, after=new)
    return RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)


@app.post("/organizer/judges")
@app.post("/api/v1/judges")
async def invite_judge(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    data, conn, actor = await body_of(request), conn_of(request), actor_of(request)
    email = clean(data.get("email"), 254).lower()
    if "@" not in email:
        return JSONResponse({"error": "invalid", "detail": "email required"}, status_code=422)
    raw_tracks = data.get("tracks") or ""
    raw_tracks = raw_tracks if isinstance(raw_tracks, list) else str(raw_tracks).split(",")
    tracks = [t.strip() for t in raw_tracks if isinstance(t, str) and t.strip()]
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        uid = row["id"] if row else "usr_" + secrets.token_hex(6)
        if row is None:
            conn.execute("INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
                         (uid, email, clean(data.get("name"), 100), format_utc(request.state.now)))
        existing = conn.execute("SELECT id FROM judges WHERE event_id = ? AND user_id = ?", (ev["id"], uid)).fetchone()
        jid = existing["id"] if existing else "jdg_" + secrets.token_hex(4)
        if not existing:
            conn.execute("INSERT INTO judges VALUES (?, ?, ?)", (jid, ev["id"], uid))
        for tr in tracks:
            if conn.execute("SELECT 1 FROM tracks WHERE id = ? AND event_id = ?", (tr, ev["id"])).fetchone():
                conn.execute("INSERT OR IGNORE INTO judge_tracks VALUES (?, ?)", (jid, tr))
        # A set-password link only for accounts without a password; never a reset of an existing one.
        link = svc.create_password_link(conn, uid)
        svc.audit(conn, actor, ev["id"], "judge.invite", jid, email=email, tracks=tracks, link_issued=bool(link))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    target = f"/organizer?event={ev['id']}"
    return RedirectResponse(target + (f"&link=/set-password/{link}" if link else ""), status_code=303)


@app.post("/organizer/assign")
@app.post("/api/v1/assignments/auto")
async def run_assignment(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    data, conn = await body_of(request), conn_of(request)
    try:
        k = max(1, min(10, int(data.get("k") or 3)))
    except (TypeError, ValueError, OverflowError):
        k = 3
    projects = [(r["id"], r["track_id"]) for r in conn.execute(
        "SELECT id, track_id FROM projects WHERE event_id = ? AND status = 'submitted' AND superseded_by IS NULL",
        (ev["id"],))]
    judge_tracks: dict[str, set[str]] = {r["id"]: set() for r in
                                         conn.execute("SELECT id FROM judges WHERE event_id = ?", (ev["id"],))}
    for r in conn.execute("SELECT jt.judge_id, jt.track_id FROM judge_tracks jt JOIN judges j ON j.id = jt.judge_id "
                          "WHERE j.event_id = ?", (ev["id"],)):
        judge_tracks[r["judge_id"]].add(r["track_id"])
    existing = [(r["judge_id"], r["project_id"]) for r in conn.execute(
        "SELECT a.judge_id, a.project_id FROM assignments a JOIN judges j ON j.id = a.judge_id WHERE j.event_id = ?",
        (ev["id"],))]
    conflicts = {(r["jid"], r["pid"]) for r in conn.execute(
        "SELECT j.id AS jid, p.id AS pid FROM judges j JOIN team_members m ON m.user_id = j.user_id "
        "JOIN projects p ON p.team_id = m.team_id WHERE j.event_id = ?", (ev["id"],))}
    new = auto_assign(projects, judge_tracks, existing, conflicts, k)
    conn.executemany("INSERT OR IGNORE INTO assignments VALUES (?, ?)", new)
    svc.audit(conn, actor_of(request), ev["id"], "assignment.run", ev["id"], k=k, added=len(new))
    return RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)


@app.post("/organizer/publish")
@app.post("/api/v1/results/publish")
async def publish(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    value = 0 if (await body_of(request)).get("unpublish") else 1
    conn = conn_of(request)
    with db.transaction(conn):
        changed = conn.execute("UPDATE events SET results_published = ? WHERE id = ? AND results_published != ?",
                               (value, ev["id"], value)).rowcount
        svc.audit(conn, actor_of(request), ev["id"], "results.publish" if value else "results.unpublish", ev["id"])
        if changed and value:
            enqueue(conn, ev["id"], "results.published", {"event": ev["id"]}, request.state.now)
    return RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)


# --- T3: community voting (contracts/t3-public.md) ------------------------------------------

def _vstate(ev, now) -> str:
    return voting_state(ev["voting_open"], ev["voting_close"], now)


def _ballot_projects(conn, event_id: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT p.id, p.title, p.summary, p.team_id, t.name AS team, COALESCE(tr.name, '') AS track "
        "FROM projects p JOIN teams t ON t.id = p.team_id LEFT JOIN tracks tr ON tr.id = p.track_id "
        "WHERE p.event_id = ? AND p.status = 'submitted' AND p.superseded_by IS NULL", (event_id,)).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _ballot(request: Request, ev) -> dict:
    conn, actor = conn_of(request), actor_of(request)
    projects = _ballot_projects(conn, ev["id"])
    mine = {r["project_id"] for r in conn.execute(
        "SELECT project_id FROM votes WHERE event_id = ? AND user_id = ?", (ev["id"], actor.user_id))}
    order = ballot_order(ev["id"], actor.user_id, projects)
    return {"event": ev["id"], "state": _vstate(ev, request.state.now),
            "voting_open": ev["voting_open"], "voting_close": ev["voting_close"],
            "votes_per_voter": ev["votes_per_voter"], "votes_left": max(0, ev["votes_per_voter"] - len(mine)),
            "can_vote": not actor.judge_id(ev["id"]),
            "projects": [{"id": p, "title": projects[p]["title"], "summary": projects[p]["summary"],
                          "team": projects[p]["team"], "track": projects[p]["track"], "voted": p in mine,
                          "own_team": actor.team_id(ev["id"]) == projects[p]["team_id"]} for p in order]}


@app.get("/api/v1/ballot")
def ballot_api(request: Request):
    ev = event_for(request)
    if ev is None:
        return deny(request, Decision.NOT_FOUND)
    if not actor_of(request).authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    return _ballot(request, ev)


@app.get("/vote", response_class=HTMLResponse)
def vote_page(request: Request):
    ev = event_for(request)
    if ev is None:
        return deny(request, Decision.NOT_FOUND)
    if not actor_of(request).authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    state = _vstate(ev, request.state.now)
    results = _vote_tally(conn_of(request), ev["id"]) if state == "closed" else None
    return render(request, "vote.html", {"ballot": _ballot(request, ev), "results": results,
                                         "titles": {p: m["title"] for p, m in
                                                    _ballot_projects(conn_of(request), ev["id"]).items()}})


def _vote_tally(conn, event_id: str) -> list[dict]:
    votes = conn.execute("SELECT user_id, project_id FROM votes WHERE event_id = ?", (event_id,)).fetchall()
    voided = {r["user_id"] for r in conn.execute("SELECT user_id FROM voided_voters WHERE event_id = ?", (event_id,))}
    return tally(((v["user_id"], v["project_id"]) for v in votes), voided)


async def _project_from_body(request: Request, project_id: str | None) -> str:
    if project_id is not None:
        return project_id
    value = (await body_of(request)).get("project")
    return value if isinstance(value, str) else ""


@app.post("/api/v1/votes")
@app.post("/vote/{project_id}")
async def cast_vote(request: Request, project_id: str | None = None):
    conn, actor, ev = conn_of(request), actor_of(request), event_for(request)
    if ev is None:
        return deny(request, Decision.NOT_FOUND)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    pid = await _project_from_body(request, project_id)
    project = _ballot_projects(conn, ev["id"]).get(pid)
    if project is None:
        return deny(request, Decision.NOT_FOUND, "not_on_ballot")
    decision, reason = authz.cast_vote(actor, ev["id"], _vstate(ev, request.state.now), project["team_id"])
    if not decision.allowed:
        return deny(request, decision, reason)
    ok, retry = request.app.state.limits["vote"].hit(actor.user_id, _mono())
    if not ok:
        return _too_many(request, retry, "too many votes")
    with db.transaction(conn):
        mine = {r["project_id"] for r in conn.execute(
            "SELECT project_id FROM votes WHERE event_id = ? AND user_id = ?", (ev["id"], actor.user_id))}
        if pid in mine:
            conflict = "already_voted"
        elif len(mine) >= ev["votes_per_voter"]:
            conflict = "vote_limit"
        else:
            conflict = None
            conn.execute("INSERT INTO votes VALUES (?, ?, ?, ?)",
                         (ev["id"], actor.user_id, pid, format_utc(request.state.now)))
            svc.audit(conn, actor, ev["id"], "vote.cast", pid)
    if conflict:
        return JSONResponse({"error": conflict}, status_code=409) if wants_json(request) else \
            render(request, "message.html", {"title": "Vote not counted", "message": conflict}, status=409)
    if wants_json(request):
        return JSONResponse({"project": pid, "voted": True}, status_code=201)
    return RedirectResponse(f"/vote?event={ev['id']}", status_code=303)


@app.delete("/api/v1/votes/{project_id}")
@app.post("/vote/{project_id}/withdraw")
def withdraw_vote(request: Request, project_id: str):
    conn, actor, ev = conn_of(request), actor_of(request), event_for(request)
    if ev is None:
        return deny(request, Decision.NOT_FOUND)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    if _vstate(ev, request.state.now) != "open":
        return deny(request, Decision.FORBIDDEN, "voting_closed")
    ok, retry = request.app.state.limits["vote"].hit(actor.user_id, _mono())
    if not ok:
        return _too_many(request, retry, "too many votes")
    with db.transaction(conn):
        removed = conn.execute("DELETE FROM votes WHERE event_id = ? AND user_id = ? AND project_id = ?",
                               (ev["id"], actor.user_id, project_id)).rowcount
        if removed:
            svc.audit(conn, actor, ev["id"], "vote.withdraw", project_id)
    if not removed:
        return deny(request, Decision.NOT_FOUND, "no_such_vote")
    if wants_json(request):
        return Response(status_code=204)
    return RedirectResponse(f"/vote?event={ev['id']}", status_code=303)


@app.get("/api/v1/votes/results")
def vote_results(request: Request):
    ev = event_for(request)
    if ev is None:
        return deny(request, Decision.NOT_FOUND)
    decision, reason = authz.read_vote_results(_vstate(ev, request.state.now))
    if not decision.allowed:
        return deny(request, decision, reason)
    return _vote_tally(conn_of(request), ev["id"])


@app.post("/organizer/voters/{user_id}/void")
@app.post("/organizer/voters/{user_id}/unvoid")
@app.post("/api/v1/voters/{user_id}/void")
@app.post("/api/v1/voters/{user_id}/unvoid")
async def set_voter_void(request: Request, user_id: str):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    conn = conn_of(request)
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        return deny(request, Decision.NOT_FOUND)
    if request.url.path.endswith("/void"):
        reason = clean((await body_of(request)).get("reason"), 1000)
        if not reason:
            return JSONResponse({"error": "invalid", "detail": "a reason is required"}, status_code=422)
        with db.transaction(conn):
            conn.execute("INSERT INTO voided_voters VALUES (?, ?, ?, ?, ?) ON CONFLICT(event_id, user_id) DO UPDATE "
                         "SET reason = excluded.reason", (ev["id"], user_id, reason, actor_of(request).user_id,
                                                          format_utc(request.state.now)))
            svc.audit(conn, actor_of(request), ev["id"], "vote.void", user_id, reason=reason)
    else:
        with db.transaction(conn):
            if conn.execute("DELETE FROM voided_voters WHERE event_id = ? AND user_id = ?",
                            (ev["id"], user_id)).rowcount:
                svc.audit(conn, actor_of(request), ev["id"], "vote.unvoid", user_id)
    return RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)


# --- T3: comments ---------------------------------------------------------------------------------------

@app.post("/projects/{project_id}/comments")
@app.post("/api/v1/projects/{project_id}/comments")
async def add_comment(request: Request, project_id: str):
    conn, actor = conn_of(request), actor_of(request)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    p = conn.execute("SELECT event_id FROM projects WHERE id = ? AND status = 'submitted'", (project_id,)).fetchone()
    if p is None:
        return deny(request, Decision.NOT_FOUND)
    raw = (await body_of(request)).get("body")
    body = raw.strip() if isinstance(raw, str) else ""
    if not 1 <= len(body) <= 2000:
        return JSONResponse({"error": "invalid", "detail": "comment must be 1 to 2000 characters"}, status_code=422)
    ok, retry = request.app.state.limits["comment"].hit(actor.user_id, _mono())
    if not ok:
        return _too_many(request, retry, "too many comments")
    with db.transaction(conn):
        cid = conn.execute("INSERT INTO comments (project_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
                           (project_id, actor.user_id, body, format_utc(request.state.now))).lastrowid
        svc.audit(conn, actor, p["event_id"], "comment.create", str(cid), project=project_id)
    if wants_json(request) or "application/json" in request.headers.get("content-type", ""):
        return JSONResponse({"id": cid, "project": project_id, "body": body}, status_code=201)
    return RedirectResponse(f"/projects/{project_id}#comments", status_code=303)


@app.delete("/api/v1/comments/{comment_id}")
@app.post("/comments/{comment_id}/delete")
def delete_comment(request: Request, comment_id: int):
    conn, actor = conn_of(request), actor_of(request)
    c = conn.execute("SELECT c.id, c.user_id, c.project_id, p.event_id FROM comments c "
                     "JOIN projects p ON p.id = c.project_id WHERE c.id = ? AND c.deleted_at IS NULL",
                     (comment_id,)).fetchone()
    if c is None:
        return deny(request, Decision.NOT_FOUND)
    decision = authz.delete_comment(actor, c["event_id"], c["user_id"])
    if not decision.allowed:
        return deny(request, decision)
    with db.transaction(conn):
        conn.execute("UPDATE comments SET deleted_at = ? WHERE id = ?", (format_utc(request.state.now), comment_id))
        svc.audit(conn, actor, c["event_id"], "comment.delete", str(comment_id), project=c["project_id"])
    if wants_json(request):
        return Response(status_code=204)
    return RedirectResponse(f"/projects/{c['project_id']}#comments", status_code=303)


# --- T4: signed records and certificates (contracts/t4-records-widget.md) ---------------------------

@app.get("/.well-known/dogfood-signing-key.pem")
def signing_key_pem():
    return Response(records.public_pem(boot.SIGNING_KEY), media_type="application/x-pem-file")


@app.post("/organizer/records/issue")
@app.post("/api/v1/records/issue")
async def issue_records(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    if not ev["results_published"]:
        return JSONResponse({"error": "results_not_published"}, status_code=409)
    conn, stamp = conn_of(request), format_utc(request.state.now)
    criteria = list(svc.rubric(conn, ev["id"]))
    subjects = []
    for r in conn.execute(
            "SELECT DISTINCT m.user_id, COALESCE(NULLIF(u.name, ''), u.email) AS name, t.name AS team, "
            "MIN(p.id) AS project, p.title FROM projects p JOIN teams t ON t.id = p.team_id "
            "JOIN team_members m ON m.team_id = t.id JOIN users u ON u.id = m.user_id "
            "WHERE p.event_id = ? AND p.status = 'submitted' AND p.superseded_by IS NULL "
            "GROUP BY m.user_id ORDER BY m.user_id", (ev["id"],)):
        subjects.append((r["user_id"], "participant", {"name": r["name"], "team": r["team"],
                                                        "project": r["project"], "project_title": r["title"]}))
    for r in conn.execute(
            "SELECT j.id, j.user_id, COALESCE(NULLIF(u.name, ''), u.email) AS name, COUNT(rv.id) AS n "
            "FROM judges j JOIN users u ON u.id = j.user_id JOIN reviews rv ON rv.judge_id = j.id "
            "WHERE j.event_id = ? GROUP BY j.id HAVING n > 0 ORDER BY j.id", (ev["id"],)):
        # Count only: which projects and what scores stay private (peer isolation).
        subjects.append((r["user_id"], "judge", {"name": r["name"], "judge_id": r["id"],
                                                  "projects_reviewed": r["n"], "criteria": criteria}))
    issued = 0
    with db.transaction(conn):
        for user_id, kind, fields in subjects:
            if conn.execute("SELECT 1 FROM records WHERE event_id = ? AND user_id = ? AND kind = ?",
                            (ev["id"], user_id, kind)).fetchone():
                continue
            rid = secrets.token_hex(16)
            record = {"type": kind, "record_id": rid, "event_id": ev["id"], "event_name": ev["name"],
                      "issued_at": stamp, **fields}
            conn.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (rid, ev["id"], user_id, kind, records.canonical(record).decode("utf-8"),
                          records.sign(boot.SIGNING_KEY, record), stamp))
            issued += 1
        svc.audit(conn, actor_of(request), ev["id"], "records.issue", ev["id"], issued=issued)
    if wants_json(request):
        return {"issued": issued}
    return RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)


def _record(conn, record_id: str):
    row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    return (row, json.loads(row["payload"])) if row else (None, None)


@app.get("/records/{record_id}.json")
def record_json(request: Request, record_id: str):
    row, record = _record(conn_of(request), record_id)
    if row is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return {"record": record, "signature": row["signature"]}


@app.get("/records/{record_id}", response_class=HTMLResponse)
def record_page(request: Request, record_id: str):
    row, record = _record(conn_of(request), record_id)
    if row is None:
        return render(request, "message.html", {"title": "Not found", "message": "No such record."}, status=404)
    return render(request, "record.html", {"r": record, "row": row})


@app.get("/verify/{record_id}", response_class=HTMLResponse)
def verify_page(request: Request, record_id: str):
    row, record = _record(conn_of(request), record_id)
    if row is None:
        return render(request, "message.html", {"title": "Not found", "message": "No such record."}, status=404)
    ok = records.verify(boot.SIGNING_KEY.public_key(), record, row["signature"])
    return render(request, "message.html", {"title": "Record " + ("valid" if ok else "INVALID"),
                                            "message": ("The signature matches this portal's public key."
                                                        if ok else "The signature does not match.")})


# --- T4: embeddable gallery widget ------------------------------------------------------------

@app.get("/embed/gallery", response_class=HTMLResponse)
def embed_gallery(request: Request):
    ev = event_for(request)
    items = svc.gallery(conn_of(request), ev["id"])[0] if ev else []
    return TEMPLATES.TemplateResponse(request, "embed.html", {"items": items, "event": ev})


WIDGET_JS = """(function () {
  var s = document.currentScript;
  var base = new URL(s.src).origin;
  var ev = s.getAttribute("data-event");
  var f = document.createElement("iframe");
  f.src = base + "/embed/gallery" + (ev ? "?event=" + encodeURIComponent(ev) : "");
  f.title = "Hackathon gallery";
  f.style.width = "100%";
  f.style.border = "0";
  f.height = s.getAttribute("data-height") || "600";
  f.loading = "lazy";
  s.parentNode.insertBefore(f, s.nextSibling);
})();
"""


@app.get("/widget.js")
def widget_js():
    return Response(WIDGET_JS, media_type="application/javascript")


# --- T4: API tokens ---------------------------------------------------------------------------------------

@app.post("/api/v1/tokens")
async def create_token(request: Request):
    actor = actor_of(request)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    name = clean((await body_of(request)).get("name"), 80) or "api"
    conn = conn_of(request)
    token = "dft_" + secrets.token_urlsafe(32)
    with db.transaction(conn):
        svc.create_session(conn, actor.user_id, label=f"api:{name}", token=token, ttl=None)
        tid = svc.token_id(token)
        svc.audit(conn, actor, None, "token.create", tid, name=name)
    return JSONResponse({"id": tid, "name": name, "token": token}, status_code=201)


@app.get("/api/v1/tokens")
def list_tokens(request: Request):
    actor = actor_of(request)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    rows = conn_of(request).execute("SELECT token_hash, label, created_at FROM sessions WHERE user_id = ? "
                                    "AND label LIKE 'api:%' ORDER BY created_at", (actor.user_id,)).fetchall()
    return [{"id": r["token_hash"][:16], "name": r["label"][4:], "created_at": r["created_at"]} for r in rows]


@app.delete("/api/v1/tokens/{token_id}")
def revoke_token(request: Request, token_id: str):
    actor = actor_of(request)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    if len(token_id) != 16 or any(ch not in "0123456789abcdef" for ch in token_id):
        return deny(request, Decision.NOT_FOUND)
    conn = conn_of(request)
    with db.transaction(conn):
        removed = conn.execute("DELETE FROM sessions WHERE user_id = ? AND label LIKE 'api:%' AND "
                               "substr(token_hash, 1, 16) = ?", (actor.user_id, token_id)).rowcount
        if removed:
            svc.audit(conn, actor, None, "token.revoke", token_id)
    return Response(status_code=204) if removed else deny(request, Decision.NOT_FOUND)


# --- T4: webhooks -------------------------------------------------------------------------------------------

@app.post("/api/v1/webhooks")
async def create_webhook(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    data, conn = await body_of(request), conn_of(request)
    events = data.get("events")
    if isinstance(events, str):
        events = [e.strip() for e in events.split(",") if e.strip()]
    error = webhooks.validate(data.get("url"), data.get("secret"), events)
    if error:
        return JSONResponse({"error": "invalid", "detail": error}, status_code=422)
    wid = "wh_" + secrets.token_hex(6)
    with db.transaction(conn):
        conn.execute("INSERT INTO webhooks VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (wid, ev["id"], data["url"], data["secret"], json.dumps(events), actor_of(request).user_id,
                      format_utc(request.state.now)))
        svc.audit(conn, actor_of(request), ev["id"], "webhook.create", wid, url=data["url"], events=events)
    body = {"id": wid, "url": data["url"], "events": events}
    return JSONResponse(body, status_code=201) if wants_json(request) else \
        RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)


@app.post("/organizer/webhooks")
async def create_webhook_form(request: Request):
    return await create_webhook(request)


@app.get("/api/v1/webhooks")
def list_webhooks(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    rows = conn_of(request).execute("SELECT id, url, events, created_at FROM webhooks WHERE event_id = ?",
                                    (ev["id"],)).fetchall()
    return [{"id": r["id"], "url": r["url"], "events": json.loads(r["events"]), "created_at": r["created_at"]}
            for r in rows]


@app.delete("/api/v1/webhooks/{webhook_id}")
def delete_webhook(request: Request, webhook_id: str):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    conn = conn_of(request)
    with db.transaction(conn):
        removed = conn.execute("DELETE FROM webhooks WHERE id = ? AND event_id = ?", (webhook_id, ev["id"])).rowcount
        if removed:
            svc.audit(conn, actor_of(request), ev["id"], "webhook.delete", webhook_id)
    return Response(status_code=204) if removed else deny(request, Decision.NOT_FOUND)


@app.get("/api/v1/webhooks/{webhook_id}/deliveries")
def webhook_deliveries(request: Request, webhook_id: str):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    conn = conn_of(request)
    if not conn.execute("SELECT 1 FROM webhooks WHERE id = ? AND event_id = ?", (webhook_id, ev["id"])).fetchone():
        return deny(request, Decision.NOT_FOUND)
    out = []
    for d in conn.execute("SELECT id, event, state, attempts, last_status, last_error, created_at FROM "
                          "webhook_deliveries WHERE webhook_id = ? ORDER BY created_at DESC LIMIT 200", (webhook_id,)):
        attempts = [dict(a) for a in conn.execute("SELECT attempt, at, status, error FROM delivery_attempts "
                                                  "WHERE delivery_id = ? ORDER BY attempt", (d["id"],))]
        out.append({**dict(d), "attempt_log": attempts})  # status codes only, never bodies
    return out


# --- T4: events, bulk export and import ------------------------------------------------------------------

@app.get("/api/v1/events")
def list_events(request: Request):
    rows = conn_of(request).execute("SELECT id, name, submissions_close, judging_close, voting_open, voting_close, "
                                    "results_published FROM events ORDER BY created_at, id").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/v1/events/{event_id}/export.json")
def export_event(request: Request, event_id: str):
    conn = conn_of(request)
    ev = svc.get_event(conn, event_id)
    if ev is None:
        return deny(request, Decision.NOT_FOUND)
    decision = authz.export_results(actor_of(request), event_id)
    if not decision.allowed:
        return deny(request, decision)
    body = svc.export_event(conn, ev, voting_closed=_vstate(ev, request.state.now) == "closed")
    svc.audit(conn, actor_of(request), event_id, "event.export", event_id)
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{event_id}.json"'})


@app.post("/api/v1/import")
async def import_event(request: Request):
    actor = actor_of(request)
    if not actor.authenticated:
        return deny(request, Decision.UNAUTHENTICATED)
    if not actor.is_admin:
        return deny(request, Decision.FORBIDDEN)
    data, conn = await body_of(request), conn_of(request)
    event = data.get("event") if isinstance(data, dict) else None
    if isinstance(event, dict) and isinstance(event.get("id"), str) and svc.get_event(conn, event["id"]):
        return JSONResponse({"error": "event_exists", "event": event["id"]}, status_code=409)
    try:
        report = importer.import_fixtures(conn, data, request.state.now, boot.invite_secret())
    except importer.FixtureError as e:
        return JSONResponse({"error": "invalid", "detail": str(e)}, status_code=422)
    except sqlite3.IntegrityError as e:  # ids that already belong to another event
        return JSONResponse({"error": "conflict", "detail": str(e)}, status_code=409)
    svc.apply_import_extras(conn, event["id"], data, actor, request.state.now)
    return JSONResponse({"event": event["id"], "counts": report.counts, "rejected": report.rejected,
                         "duplicates": report.duplicates}, status_code=201)


@app.get("/api/v1/results")
def results_api(request: Request):
    """Judged ranking: public once published, always visible to organizers."""
    ev = event_for(request)
    if ev is None:
        return deny(request, Decision.NOT_FOUND)
    if not ev["results_published"] and not authz.export_results(actor_of(request), ev["id"]).allowed:
        return deny(request, Decision.FORBIDDEN, "results_not_published")
    res, meta = svc.results(conn_of(request), ev["id"])
    return [{"rank": r.rank, "project": r.project, "title": meta[r.project]["title"], "team": meta[r.project]["team"],
             "n_reviews": r.n_reviews} for r in res]
