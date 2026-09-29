# Contract: core/authz (authentication + role isolation)

## Purpose
Turns a request's credentials into an actor, then answers "may this actor do
this action on this resource" before any data is read. Handlers only ever call
authz and act on its decision; they never re-derive permissions. Login UI and
session creation are handed off to the login flow in `lifecycle.md`.

## Inputs
- `credentials`: `Authorization: Bearer <token>` header or `session` cookie; may be absent or garbage
- `action`: one of the actions in the matrix below
- `resource`: e.g. target judge id, project id, event id

## Outputs
- `actor`: `{user_id, roles: set[visitor|participant|judge|organizer|admin], judge_id?, team_ids}`
- decision: `allow` | `deny(401 unauthenticated)` | `deny(403 forbidden)` | `deny(404 not_found)`

## Roles
visitor (no valid session), participant, judge, organizer, admin. A user can hold
several roles. admin implies organizer. Role is looked up server-side from the
session row, never from anything the client sends (no `?role=`, no `X-Role`).

## Behavior cases (input -> expected output)
| # | Actor | Action | Expected | Notes |
|---|-------|--------|----------|-------|
| 1 | visitor | view gallery | allow | |
| 2 | visitor | read own judge scores | 401 | |
| 3 | participant | read own judge scores | 403 | not a judge |
| 4 | organizer (not a judge) | read own judge scores | 403 | organizer uses case 10 instead |
| 5 | judge jdg_24 | read own judge scores | allow; data filtered to `judge_id = jdg_24` | filter is in the query, not the template |
| 6 | judge jdg_24 | read own scores with `?judge=jdg_26` added | allow, still only jdg_24's rows | query param ignored on the "own" route |
| 7 | judge jdg_24 | read judge jdg_24's scores (by id) | allow | |
| 8 | judge jdg_26 | read judge jdg_24's scores (by id) | 403 | acceptance check 5 |
| 9 | judge jdg_26 | read judge `jdg_999` (nonexistent) | 403 | same as 8, so ids cannot be enumerated |
| 10 | organizer | read judge jdg_24's scores | allow | |
| 11 | participant | export CSV | 403 | |
| 12 | judge | export CSV | 403 | |
| 13 | organizer | export CSV | allow | |
| 14 | any | token `demo-*` when `DOGFOOD_DEMO_SESSIONS` unset | 401 | demo tokens only exist when explicitly enabled |
| 15 | visitor | export CSV | 401 | |
| 16 | judge jdg_24 | read a single score row owned by jdg_26 by score id | 403 | no side door via row ids |

## Edge cases that must be covered
- Header present but empty, `Bearer` with no token, token with trailing space, unknown token: all visitor -> 401 on protected actions.
- Both cookie and Bearer present and pointing at different users: Bearer wins, and it is logged.
- Expired or revoked session: visitor.
- Judge id comparison is exact string match (`jdg_24` != `JDG_24` != `jdg_24 `).
- Path traversal / encoded ids (`/api/judges/jdg_24%2F..`) never resolve to another judge.
- A judge who is also a participant on some team keeps both roles; being a judge never grants participant actions on teams they are not in.

## Explicitly out of scope
- Password hashing and the login form: `lifecycle.md` cases 1 to 5.
- Rate limiting (T3 anti-abuse).

## Status
Implemented. Tests: test_authz.py, test_authz_sessions.py. The design decisions in it were approved before implementation; the cases were not reviewed one by one.
