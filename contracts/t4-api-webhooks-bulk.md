# Contract: T4 REST API, webhooks, bulk import/export

## Purpose
A documented, versioned REST API (`/api/v1`, OpenAPI at `/openapi.json`) so every
organizer, judge and participant action can be scripted; signed webhooks so other
systems hear about events; and a lossless bulk export/import so an event can be
moved in and out of the portal. Authorization is unchanged: every API route asks
`core/authz` exactly as its HTML twin does.

## Inputs
- API tokens: organizer creates a named personal token (`POST /api/v1/tokens`), shown once, stored hashed, revocable; used as `Authorization: Bearer <token>`. Acts with the creator's roles.
- Webhook: `{url (http/https), secret (>= 16 chars), events: [..]}`, organizer only
- Import file: JSON in the `fixtures.json` shape (the export format is a superset of it). Import is admin-only, since it creates an event (clarified 2026-09-27). An empty portal is started with `DOGFOOD_FIXTURES=none`; its first admin is created with `python -m dogfood.cli create-admin <email>`.

## Outputs
- `/api/v1/...` JSON routes for: events, projects (list/create/edit), teams (create/join), judges (invite/list), assignments (auto-assign), scores (own/by judge/submit), results, confidence, agreement, votes, comments, exports
- Webhook deliveries: `POST <url>`, body `{"id", "event", "created_at", "data"}`, header `X-Dogfood-Signature: sha256=<hex HMAC-SHA256(secret, raw body)>`
- `GET /api/v1/events/{id}/export.json`, `POST /api/v1/import`

## Webhook events
`project.submitted`, `project.updated`, `score.submitted` (judge id and project id only, never values), `results.published`, `voting.closed` (sent by the delivery worker within about a second of close; clarified 2026-09-27).

## Behavior cases (input -> expected output)
| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | `GET /openapi.json` | 200, lists every `/api/v1` route with request/response schemas | FastAPI-generated |
| 2 | organizer creates a token, uses it on `/api/export.csv` | 201 with the token once; then 200 | |
| 3 | same token after `DELETE /api/v1/tokens/{id}` | 401 | revocation |
| 4 | participant or judge creates a token | 201, token carries only their own roles | a judge token still cannot read peer scores (403) |
| 5 | every existing HTML/JSON authz case (authz.md 1-16) via its `/api/v1` twin | identical status codes | no side door |
| 6 | organizer registers webhook for `score.submitted`; a judge scores | exactly one delivery; signature verifies with the secret; body has no score values | peer-score isolation holds through webhooks |
| 7 | receiver returns 500 or is unreachable | retried after 10 s, 60 s, 300 s, then marked failed; every attempt in the delivery log | never blocks the request that caused it |
| 8 | webhook URL not http(s), or secret shorter than 16 | 422 | |
| 9 | non-organizer registers a webhook | 403 | |
| 10 | no webhooks configured | no outbound network traffic at all | offline rule |
| 11 | export the fixture event, import it into an empty portal | the imported event has the same projects, teams, judges, tracks, scores and the same ranking (all 40 ranks identical) | lossless round trip |
| 12 | import a file whose event id already exists | 409, nothing written | |
| 13 | import a malformed file | 422 with the importer's reasons; nothing written | reuses core/importer, one transaction |
| 14 | export as judge or participant | 403 | contains every score |
| 15 | export content | fixture-shaped keys plus `rubric` (names and weights), `exclusions`, `votes` (after close only), `comments`; no password hashes, no session tokens, no API tokens | |

## Edge cases that must be covered
- Webhook secret is never returned after creation; the delivery log shows status codes, not bodies.
- A webhook URL pointing at the portal itself is allowed (useful for testing) and must not deadlock.
- API tokens are exempt from the cookie CSRF check (browsers never attach them automatically).
- The acceptance checker's routes and `.dogfood.toml` are unchanged.
- Added 2026-09-27: import refuses (409 `ids_in_use`) a file whose track/judge/team/project ids belong to another event; a failure after the core import undoes the whole event (422).
- Added 2026-09-27: export also carries `assignments` (pending ones too), `drafts`, `organizers` and `voided_voters`; import restores those plus `comments`, `votes` and the rubric weights (the rubric itself when no scores exist yet).
- Added 2026-09-27: deliveries are sent up to 8 at a time (a dead receiver cannot delay the others); redirects count as failed attempts and are not followed; link-local (cloud metadata), multicast and unspecified addresses are refused at creation; finished deliveries older than 30 days are pruned.

## Explicitly out of scope
- OAuth / third-party apps; per-token scopes narrower than the creator's roles.
- Importing into an existing event (merge).

## Status
- [x] Drafted
- [x] Reviewed by a human (approved 2026-09-27)
- [x] Implementation matches this contract
- [x] Golden tests exist for every behavior case above
