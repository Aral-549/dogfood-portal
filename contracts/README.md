# Contracts index

Status: decisions approved by the human 2026-09-27 (stack, duplicate merge, normalization).
Per-case review of each contract still pending.

Target: DOGFOOD 2026. Claim T1 + T2 honestly; T3/T4 only after T2 is verified clean.
Code freeze: Tue 29 Sep 2026 18:00 UTC.

## Safety-critical path ("core")

Everything under `src/dogfood/core/` is safety-critical and needs a matching
change in `tests/golden/` or `contracts/` for every edit:

| Module            | Contract                    | Why it is critical                          |
|-------------------|-----------------------------|---------------------------------------------|
| `core/authz`      | `authz.md`                  | Role isolation is the most-penalised check  |
| `core/deadline`   | `submissions.md`            | "Deadline enforcement that actually holds"  |
| `core/scoring`    | `scoring.md`                | Weighted rubric + normalization (25% score) |
| `core/importer`   | `fixtures-import.md`        | Every other stage trusts its output         |
| `core/csv`        | `csv-export.md`             | Organizer-facing output, injection risk     |

Non-core: `gallery.md` (public read path), `acceptance.md` (the external checker contract).

## Pipeline and stage logging (AGENTS.md rule 5)

```
fixtures.json -> importer -> db -> [request -> auth -> authz -> handler] -> response / CSV
                                                         \-> scoring -> CSV / dashboard
```

Each arrow is a stage boundary. Each boundary emits one structured JSON log line
(`stage`, `event`, input summary, output summary, `request_id` where applicable).
No `print` debugging. Session tokens and emails are never logged in full.

## Cross-cutting rules

- All timestamps stored and compared as UTC. Core functions take `now` as a
  parameter; nothing in `core/` reads the wall clock directly.
- Authorization is decided in the backend by `core/authz`, before any data is
  loaded. Templates never decide who may see what.
- Every id from fixtures is kept as-is (strings like `prj_07`).

## Stack (approved 2026-09-27)

Python 3.12, FastAPI, SQLite (stdlib `sqlite3`, one file on a volume), Jinja2
server-rendered HTML, one container. Rationale: single process, no DB
container, runs offline once the image is built, and the checker is already
Python. Alternative if preferred: Node/TypeScript + Postgres.

## Decisions (approved 2026-09-27)

1. Duplicate submission: merge into the latest (`prj_41`). Raw review rows are
   kept on their original project; the merge is computed by
   `core/scoring.effective_reviews` so it stays auditable. See
   `fixtures-import.md` cases 6-8.
2. Normalization: per-judge, per-criterion z-score; uninformative judges
   contribute 0. See `scoring.md`.
3. Demo session tokens are fixed strings so `.dogfood.toml` can hold them.
   Proposed: only created when `DOGFOOD_DEMO_SESSIONS=1` (on in the shipped
   compose file, off by default otherwise). See `authz.md` case 14.

## Added by the kickoff deck (problem statement)

- Ten stages: registration, teams, submissions, eligibility, judge assignment,
  scoring, normalization, results, export. Eligibility and results/publish need
  contracts in the next batch.
- "An authentication demo that stops at the login screen" is out of scope, so
  real login + sessions are required for T1.
- Normalization Proof bonus: show raw, normalized, and the ranking change.

## Beyond the problem statement (drafted 2026-09-27, awaiting review)

- `confidence.md`: prize-line confidence (bootstrap P(top k)), close-call flags,
  tie-breaker judge assignment.
- `judge-agreement.md`: leave-one-out judge agreement, outlier and favoritism
  flags, audited organizer exclusion disclosed on public results.

## Not yet contracted (next batch, after this one is reviewed)

Event creation/edit UI, team invite links, judge invitation + assignment
algorithm, live organizer progress dashboard, login/logout flow, audit log
viewer, all of T3 and T4.
