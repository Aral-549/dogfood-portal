# Contracts

Each file here is a spec written before the code it describes: what the component takes in,
what it must return, and a numbered table of cases (input and expected output). The tests in
`tests/golden/` are written against these tables rather than against the code, so a test that
disagrees with the code points at a bug, not at a test to be adjusted.

The rules we held ourselves to:

1. No module without a contract. If the behaviour changes, the contract changes in the same
   commit.
2. The agent that writes a module does not write its tests. A separate pass writes them from
   the contract and tries to break the code.
3. Tests in `tests/golden/` are frozen. New cases can be added; existing ones change only with
   an explicit decision, and each such change is noted in `BUGLOG.md` or the contract.
4. Every bug gets a `BUGLOG.md` entry and a test that reproduces it before it counts as fixed.

## Index

| Contract | Covers | Tier |
|---|---|---|
| `acceptance.md` | What the official `run.py` sends and what the portal must answer | T1, T2 |
| `authz.md` | Who may do what; role isolation decided in the backend | T1, T2 |
| `submissions.md` | Creating and editing projects; the deadline | T1 |
| `gallery.md` | The public project list, search and filter | T1 |
| `lifecycle.md` | Login, teams, events, judge invites and assignment, dashboard, publishing, eligibility | T1, T2 |
| `fixtures-import.md` | Loading `fixtures.json`, including the duplicate submission | T1 |
| `scoring.md` | Weighted rubric and per-judge normalization | T2 |
| `csv-export.md` | The results CSV | T2 |
| `confidence.md` | How sure the ranking is at the prize line; tie-breaker judges | T2, beyond the brief |
| `judge-agreement.md` | Judges who disagree with everyone else; audited exclusion | T2, beyond the brief |
| `research-advisory.md` | Recusal, ordering-based ranking, integrity checks, judging plan | T2, beyond the brief |
| `t3-public.md` | Community vote, comments, hidden tallies, ballot order, abuse limits | T3 |
| `t4-api-webhooks-bulk.md` | REST API, tokens, webhooks, export and import | T4 |
| `t4-records-widget.md` | Signed certificates and judge records, gallery widget | T4 |

Each contract ends with a status line saying which tests cover it and how it was reviewed.
Two are not fully covered: `gallery.md` has no dedicated tests for search, filter or paging,
and `research-advisory.md` was written and tested by the same agent.

## The safety-critical code

Everything in `src/dogfood/core/` is pure logic with no web or clock access, and each module
maps to a contract:

| Module | Contract | Why it matters |
|---|---|---|
| `core/authz` | `authz.md` | Role isolation is the check that costs teams the most points |
| `core/deadline` | `submissions.md` | The deadline has to hold against curl, not just the form |
| `core/scoring` | `scoring.md` | The ranking itself |
| `core/importer` | `fixtures-import.md` | Everything downstream trusts its output |
| `core/csvexport` | `csv-export.md` | Organizer-facing output, with a formula-injection risk |
| `core/confidence` | `confidence.md` | Prize-line confidence shown to organizers |
| `core/agreement` | `judge-agreement.md` | The evidence behind excluding a judge |
| `core/assignment` | `lifecycle.md` case 14 | Who judges what; conflicts are never paired |
| `core/timeutil` | the rules below | Every stored and compared timestamp |
| `core/public` | `t3-public.md` | Voting window, ballot order, tallies |
| `core/ratelimit` | `t3-public.md` cases 18 to 20 | Abuse limits and login throttling |
| `core/records` | `t4-records-widget.md` | Signatures anyone can check offline |
| `core/webhooks` | `t4-api-webhooks-bulk.md` | Signed payloads that never carry score values |
| `core/ordinal`, `core/integrity`, `core/planning` | `research-advisory.md` | Advisory tools for the organizer |

## Rules that apply everywhere

- Timestamps are stored and compared in UTC. Core functions take `now` as an argument; nothing
  in `core/` reads the clock.
- Authorization is decided by `core/authz` before any data is loaded. Templates only decide
  which links to show.
- Ids from the fixtures are kept as they are (`prj_07` stays `prj_07`).
- Each pipeline stage logs one structured JSON line at its boundary
  (`fixtures.json -> importer -> db -> request -> auth -> authz -> handler -> response`), with a
  request id. Tokens are logged only as their first four characters.

## Decisions made up front

- Stack: Python 3.12, FastAPI, SQLite in one file, server-rendered HTML, one container. One
  process and no database container, it runs offline once built, and the checker is Python too.
- The duplicate submission (`prj_07` and `prj_41`, same team, title and repository) is merged
  into the later one. The raw reviews stay on their original project and the merge happens at
  scoring time, so it can be audited and undone. See `fixtures-import.md` cases 6 to 8.
- Normalization is a per-judge, per-criterion z-score, and judges who carry no information
  (one review, or the same score everywhere) contribute nothing. See `scoring.md`.
- Demo tokens are fixed strings so `.dogfood.toml` can hold them, and they only exist when
  `DOGFOOD_DEMO_SESSIONS=1`. See `authz.md` case 14.
