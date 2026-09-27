"""HTTP layer. Every protected handler asks core.authz first and acts on the Decision."""

import json
import os
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import boot, logs, services as svc
from .core import authz, csvexport
from .core.assignment import auto_assign
from .core.authz import Actor, Decision
from .core.deadline import submissions_open
from .core.timeutil import format_utc, parse_utc

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SESSION_COOKIE = "session"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logs.setup(os.environ.get("DOGFOOD_LOG_LEVEL", "INFO"))
    app.state.conn = boot.boot()
    yield
    app.state.conn.close()


app = FastAPI(title="DOGFOOD portal", lifespan=lifespan)


# --- request plumbing ------------------------------------------------------------

@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = uuid.uuid4().hex[:12]
    request.state.request_id = rid
    conn: sqlite3.Connection = request.app.state.conn
    now = svc.utcnow()
    token, source = authz.extract_token(request.headers.get("authorization"), request.cookies.get(SESSION_COOKIE))
    actor = svc.load_actor(conn, token, now)
    request.state.actor, request.state.token, request.state.now = actor, token, now
    logs.stage("auth", "actor", request_id=rid, method=request.method, path=request.url.path,
               token=logs.redact(token), source=source, user=actor.user_id)
    if request.method in UNSAFE and source == "cookie" and not _same_origin(request):
        logs.stage("auth", "csrf_block", request_id=rid, origin=request.headers.get("origin"))
        return JSONResponse({"error": "cross_origin"}, status_code=403)
    response = await call_next(request)
    logs.stage("http", "response", request_id=rid, status=response.status_code)
    return response


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # non-browser client; SameSite=Lax already blocks cross-site cookie POSTs
    return urlsplit(origin).netloc == request.headers.get("host")


def conn_of(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


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


async def body_of(request: Request) -> dict:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    form = await request.form()
    return {k: v for k, v in form.items()}


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
        return max(1, int(raw or 1))
    except ValueError:
        return 1


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
    return render(request, "results.html", {"results": res, "meta": meta})


# --- accounts --------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/me"):
    return render(request, "login.html", {"next": next if next.startswith("/") and not next.startswith("//") else "/me"})


@app.post("/login")
async def login(request: Request):
    data = await body_of(request)
    email, password = clean(data.get("email"), 254).lower(), data.get("password") or ""
    nxt = data.get("next") or "/me"
    nxt = nxt if isinstance(nxt, str) and nxt.startswith("/") and not nxt.startswith("//") else "/me"
    conn = conn_of(request)
    row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    if not svc.check_password(password, row["password_hash"] if row else None):
        logs.stage("auth", "login_failed", request_id=request.state.request_id)
        return render(request, "login.html", {"error": "Wrong email or password.", "next": nxt}, status=401)
    token = svc.create_session(conn, row["id"])
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=14 * 86400)
    return resp


@app.post("/logout")
def logout(request: Request):
    if request.state.token:
        svc.delete_session(conn_of(request), request.state.token)
    resp = RedirectResponse("/projects", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return render(request, "register.html")


@app.post("/register")
async def register(request: Request):
    data = await body_of(request)
    email, name, password = clean(data.get("email"), 254).lower(), clean(data.get("name"), 100), data.get("password") or ""
    if "@" not in email or len(password) < 8:
        return render(request, "register.html", {"error": "Valid email and a password of 8+ characters required."},
                      status=422)
    conn = conn_of(request)
    uid = "usr_" + secrets.token_hex(6)
    try:
        conn.execute("INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                     (uid, email, name, svc.hash_password(password), format_utc(request.state.now)))
    except sqlite3.IntegrityError:
        return render(request, "register.html", {"error": "That email already has an account."}, status=409)
    token = svc.create_session(conn, uid)
    resp = RedirectResponse("/me", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=14 * 86400)
    return resp


@app.get("/set-password/{token}", response_class=HTMLResponse)
def set_password_page(request: Request, token: str):
    return render(request, "set_password.html", {"token": token})


@app.post("/set-password/{token}")
async def set_password(request: Request, token: str):
    data = await body_of(request)
    password = data.get("password") or ""
    if len(password) < 8:
        return render(request, "set_password.html", {"token": token, "error": "8+ characters."}, status=422)
    uid = svc.consume_password_link(conn_of(request), token, password)
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
    return render(request, "me.html", {"user": user, "team": team, "projects": projects, "is_open": is_open})


# --- teams ------------------------------------------------------------------------------

@app.post("/teams")
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
    return render(request, "project.html", {"p": p, "can_edit": can_edit})


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
    if wants_json(request):
        return {"id": project_id, **fields}
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# --- judging -------------------------------------------------------------------------------

@app.get("/api/judge/scores")
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
async def submit_score(request: Request, project_id: str):
    conn, actor = conn_of(request), actor_of(request)
    p = conn.execute("SELECT event_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if p is None:
        return deny(request, Decision.NOT_FOUND)
    ev = svc.get_event(conn, p["event_id"])
    jid = actor.judge_id(ev["id"])
    assigned = bool(jid) and conn.execute("SELECT 1 FROM assignments WHERE judge_id = ? AND project_id = ?",
                                          (jid, project_id)).fetchone() is not None
    decision, reason = authz.score_project(actor, ev["id"], assigned, svc.judging_open(ev, request.state.now))
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
    res, meta = svc.results(conn, ev["id"])
    duplicates = [(pid, m["superseded_by"]) for pid, m in meta.items() if m["superseded_by"]]
    audit_rows = conn.execute("SELECT * FROM audit_log WHERE event_id = ? OR event_id IS NULL ORDER BY id DESC LIMIT 50",
                              (ev["id"],)).fetchall()
    judges = conn.execute("SELECT j.id, u.email FROM judges j JOIN users u ON u.id = j.user_id WHERE j.event_id = ?",
                          (ev["id"],)).fetchall()
    tracks = conn.execute("SELECT id, name FROM tracks WHERE event_id = ? ORDER BY id", (ev["id"],)).fetchall()
    return render(request, "organizer.html", {
        "results": res, "meta": meta, "progress": svc.progress(conn, ev["id"]), "duplicates": duplicates,
        "rubric": svc.rubric(conn, ev["id"]), "audit": audit_rows, "judges": judges, "tracks": tracks,
        "links": request.query_params.get("link")})


@app.post("/organizer/event")
async def update_event(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    data, conn = await body_of(request), conn_of(request)
    changes = {}
    try:
        for key in ("submissions_close", "judging_close"):
            raw = clean(data.get(key), 40)
            if raw:
                changes[key] = format_utc(parse_utc(raw))
    except ValueError as e:
        return JSONResponse({"error": "invalid", "detail": str(e)}, status_code=422)
    for key in ("name", "prizes"):
        if isinstance(data.get(key), str):
            changes[key] = clean(data[key], 2000)
    for key, value in changes.items():
        conn.execute(f"UPDATE events SET {key} = ? WHERE id = ?", (value, ev["id"]))  # key from fixed tuple above
    svc.audit(conn, actor_of(request), ev["id"], "event.update", ev["id"],
              before={k: ev[k] for k in changes}, after=changes)
    return RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)


@app.post("/organizer/events")
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
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO events (id, name, submissions_close, judging_close, prizes, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (eid, name, close, jclose, clean(data.get("prizes"), 2000),
                                                format_utc(request.state.now)))
    conn.execute("INSERT INTO organizers VALUES (?, ?)", (eid, actor.user_id))
    for i, tname in enumerate(t.strip() for t in clean(data.get("tracks"), 2000).split(",") if t.strip()):
        conn.execute("INSERT INTO tracks VALUES (?, ?, ?)", (f"{eid}_trk_{i + 1:02d}", eid, tname[:100]))
    for pos, crit in enumerate(("functionality", "quality", "innovation")):
        conn.execute("INSERT INTO criteria VALUES (?, ?, 1, ?)", (eid, crit, pos))
    conn.execute("COMMIT")
    svc.audit(conn, actor, eid, "event.create", eid, name=name)
    return RedirectResponse(f"/organizer?event={eid}", status_code=303)


@app.post("/organizer/rubric")
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
        if not w > 0 or w != w or w == float("inf"):
            return JSONResponse({"error": "invalid", "detail": f"weight for {name} must be > 0"}, status_code=422)
        new[name] = w
    for name, w in new.items():
        conn.execute("UPDATE criteria SET weight = ? WHERE event_id = ? AND name = ?", (w, ev["id"], name))
    svc.audit(conn, actor_of(request), ev["id"], "rubric.update", ev["id"], before=current, after=new)
    return RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)


@app.post("/organizer/judges")
async def invite_judge(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    data, conn = await body_of(request), conn_of(request)
    email = clean(data.get("email"), 254).lower()
    if "@" not in email:
        return JSONResponse({"error": "invalid", "detail": "email required"}, status_code=422)
    uid = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    uid = uid["id"] if uid else "usr_" + secrets.token_hex(6)
    conn.execute("INSERT OR IGNORE INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
                 (uid, email, clean(data.get("name"), 100), format_utc(request.state.now)))
    existing = conn.execute("SELECT id FROM judges WHERE event_id = ? AND user_id = ?", (ev["id"], uid)).fetchone()
    jid = existing["id"] if existing else "jdg_" + secrets.token_hex(4)
    if not existing:
        conn.execute("INSERT INTO judges VALUES (?, ?, ?)", (jid, ev["id"], uid))
    tracks = data.get("tracks") or ""
    for tr in (tracks if isinstance(tracks, list) else str(tracks).split(",")):
        if conn.execute("SELECT 1 FROM tracks WHERE id = ? AND event_id = ?", (tr.strip(), ev["id"])).fetchone():
            conn.execute("INSERT OR IGNORE INTO judge_tracks VALUES (?, ?)", (jid, tr.strip()))
    link = svc.create_password_link(conn, uid)
    svc.audit(conn, actor_of(request), ev["id"], "judge.invite", jid, email=email)
    return RedirectResponse(f"/organizer?event={ev['id']}&link=/set-password/{link}", status_code=303)


@app.post("/organizer/assign")
async def run_assignment(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    data, conn = await body_of(request), conn_of(request)
    try:
        k = max(1, min(10, int(data.get("k") or 3)))
    except ValueError:
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
async def publish(request: Request):
    ev, denied = _organizer_guard(request)
    if denied:
        return denied
    value = 0 if (await body_of(request)).get("unpublish") else 1
    conn_of(request).execute("UPDATE events SET results_published = ? WHERE id = ?", (value, ev["id"]))
    svc.audit(conn_of(request), actor_of(request), ev["id"], "results.publish" if value else "results.unpublish",
              ev["id"])
    return RedirectResponse(f"/organizer?event={ev['id']}", status_code=303)
