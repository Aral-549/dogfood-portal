# Data model

SQLite, one file (`/data/dogfood.db` in the container, on a named volume). The
full DDL is `src/dogfood/schema.sql`, which is applied idempotently at boot.

## Entities

```
users ──< sessions
  │
  ├──< organizers >── events ──< tracks ──< judge_tracks >── judges
  │                     │                                     │
  ├──< team_members >── teams ──< projects ──< assignments >──┤
  │                     │           │                          │
  │                     │           └──< reviews >─────────────┘
  │                     │                  └──< review_scores
  └── judges            └── criteria (per event: name, weight)

audit_log (append-only), password_links (one-time set-password tokens)
```

| Table           | Key                        | Notes |
|-----------------|----------------------------|-------|
| `users`         | `id`                       | One row per person across events. `email` is unique, case-insensitive. `password_hash` is scrypt, or NULL for users who have no password yet. |
| `sessions`      | `token_hash`               | SHA-256 of the token; the token itself is never stored. `label = 'demo'` marks demo tokens, which are deleted at boot unless demo mode is on. |
| `events`        | `id`                       | `submissions_close` and `judging_close` are ISO 8601 UTC strings (`...Z`). `results_published` gates `/results`. |
| `organizers`    | `(event_id, user_id)`      | Roles are per event. Admin (`users.is_admin`) implies organizer everywhere. |
| `judges`        | `id` (fixture id kept)     | One judge row per user per event. |
| `teams`         | `id`                       | `invite_code` is unguessable (HMAC-style hash for fixture teams, random for new ones). |
| `team_members`  | `(team_id, user_id)`       | `UNIQUE(event_id, user_id)`: one team per person per event, enforced by the database. |
| `projects`      | `id`                       | `status` is `draft` or `submitted`. `superseded_by` points a duplicate at its canonical project. |
| `criteria`      | `(event_id, name)`         | `weight > 0`, enforced by a CHECK constraint. |
| `assignments`   | `(judge_id, project_id)`   | Who should score what. |
| `reviews`       | `id`, `UNIQUE(judge_id, project_id)` | One review per judge per project; re-scoring is an upsert. |
| `review_scores` | `(review_id, criterion)`   | `value BETWEEN 1 AND 5`, enforced by a CHECK constraint. |
| `audit_log`     | `id`                       | Who did what, when, with a JSON detail. Written for every state change by an organizer, judge or participant. |

### Why this shape

- **Scores are rows, not a JSON blob.** Each criterion value is a row, so the
  database enforces the 1..5 range, and adding a criterion mid-event needs no
  migration.
- **Roles live in relation tables, not a `role` column.** One person can
  organize one event and judge another, or be a participant and a judge.
  Authorization reads these tables server-side for every request.
- **Duplicates are linked, not deleted.** `superseded_by` keeps the evidence.
  The merge happens at scoring time (`core/scoring.effective_reviews`), so
  it is reversible and auditable.
- **Fixture ids are kept.** `prj_07` in the fixture is `prj_07` in the
  database, in the CSV and in the URLs. New rows get random ids with the same
  prefixes.

## The way in: `fixtures.json`

`src/dogfood/core/importer.py` (spec: `contracts/fixtures-import.md`) runs at
boot when the fixture event is not already in the database.

- Runs in one transaction. An unusable file (missing, not JSON, no event,
  bad close date) stops the boot, so the portal never starts half-seeded.
- Bad rows (unknown judge or project, out-of-range values, missing criteria)
  are rejected one at a time, with a reason in the import report and the
  audit log.
- Idempotent on fixture ids: re-importing the same file changes nothing.
- Duplicate detection: same team AND (same case-folded title OR same repo
  URL). The later submission becomes canonical.

## The way out

- `GET /api/export.csv`: ranked results (organizer).
- `GET /api/judges/{id}/scores`, `GET /api/judge/scores`: raw reviews as JSON.
- `GET /api/projects`: public gallery as JSON.
- The database is a single SQLite file. `docker compose cp portal:/data/dogfood.db .`
  gives you everything, readable with any SQLite tool.
