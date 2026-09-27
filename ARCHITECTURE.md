# Architecture

One process, one container, one SQLite file.

```
browser / curl / run.py
        │  HTTP (cookie session or Bearer token)
        ▼
┌─────────────────────────── FastAPI (src/dogfood/app.py) ──────────────────────────┐
│ middleware: request id -> extract token -> load Actor from sessions -> CSRF check │
│ handler: ask core.authz -> (deny 401/403) or query -> render Jinja2 / JSON / CSV   │
└──────────────┬───────────────────────────────┬────────────────────────────────────┘
               │                               │
      src/dogfood/core/ (pure, safety-critical) │  src/dogfood/services.py (SQL)
        authz.py     who may do what            │
        deadline.py  open iff now < close       │
        scoring.py   rubric + z-score normalize │
        assignment.py judge -> project          │
        csvexport.py results CSV                │
        importer.py  fixtures.json -> db        │
               │                               ▼
               └──────────────────────► SQLite (/data/dogfood.db, WAL)
```

## Decisions

**Authorization is a function call, not a template condition.** Every
protected handler calls one `core.authz` function *before* it touches data,
and returns whatever `Decision` it gets. Templates only choose which links to
show; they never decide access. That is why a curl request gets the same
answer as the UI.

**`core/` has no clock and no I/O** (except the importer, which writes rows).
Deadline and scoring functions take `now` and plain data as arguments. That
makes them testable at exact boundaries (one second before the close, exactly
at it, one second after) without faking the system clock.

**SQLite, not Postgres.** One file, no second container, starts in under a
second, runs with the network off, and backs up with `cp`. WAL mode handles the
concurrency of a hackathon (dozens of judges scoring at once) comfortably. The
schema is plain SQL with no SQLite-only types, so moving to Postgres later
would mostly mean changing the connection.

**Server-rendered HTML.** No build step, no JS bundle, works without
JavaScript. Every HTML action also has a JSON equivalent for scripts.

**Sessions.** Random 256-bit tokens. Only their SHA-256 is stored. Cookies are
HttpOnly and SameSite=Lax. On top of that, a cookie-authenticated POST whose
`Origin`/`Referer` host differs from ours is refused (CSRF). Bearer tokens
(API use) are not subject to the Origin check because browsers never attach
them automatically.

**Demo mode** (`DOGFOOD_DEMO_SESSIONS=1`, on in the shipped compose file)
creates four fixed tokens and demo passwords so the acceptance checker and the
demo video work out of the box. Without it, those tokens are deleted at boot.
Turn it off for a real event.

## Stage logging

Each stage boundary writes one JSON log line to stderr: `auth` (token source
and user per request), `authz` (every denial with its reason), `importer`
(input counts, then output counts, rejections and duplicates), `scoring`
(projects, reviews, merges), `audit` (every state change) and `http` (status).
Every request line carries a `request_id`. Tokens are logged only as their
first 4 characters.

## Boot sequence

1. Create `/data` and a persistent secret (`/data/secret`) on first run.
2. Apply the schema (`CREATE TABLE IF NOT EXISTS`).
3. If the fixture event is not in the database, import `fixtures.json` in one
   transaction and record the import in the audit log.
4. In demo mode, create the demo organizer and the four demo sessions, and
   print the headers for `.dogfood.toml`.
5. Serve on port 8080. `/healthz` backs the Docker healthcheck.
