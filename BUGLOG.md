# Bug Log

Every entry here must result in a permanent case added to `tests/golden/`
before it's marked resolved. A patched bug without a regression case is not
resolved -- it's just hidden until the next rewrite.

Entries below come from the 2026-09-27 adversarial pass. Regression cases are in
`tests/golden/test_regressions.py` (named `test_bugN_*`) and passed after the fixes of 2026-09-27 (174/175 golden tests; see the note on
`test_case4_body_is_a_json_list` in `contracts/acceptance.md`).

---

## 2026-09-27 -- BUG-1 demo password survives turning demo mode off
- **Symptom:** after one boot with `DOGFOOD_DEMO_SESSIONS=1`, a reboot on the same volume without it still accepts `organizer@dogfood.local` / `dogfood-demo` at `POST /login` (303 + session, admin), and the same for the demo judge/participant accounts. Only the demo bearer tokens are revoked.
- **Root cause:** `boot.py:50` deletes demo sessions only; `boot.py:70` wrote a known password hash (and `is_admin=1` user) that is never cleared.
- **Stage/module:** boot / auth
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug1_demo_password_dead_after_demo_mode_disabled`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-2 one bad criterion name rejects every score
- **Symptom:** a fixture with one misspelled criterion (`qualty`), or one unknown-judge row carrying an extra key, imports 0 of 126 scores (all rejected "missing rubric criteria"); the bad name is stored as a rubric criterion.
- **Root cause:** `core/importer.py:165` builds the rubric from the union of keys in every score row, including rows that are later rejected.
- **Stage/module:** importer
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug2_*` (2 cases)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-3 non-string reference crashes the whole import
- **Symptom:** a score with `"judge": ["jdg_01"]` or `"project": {...}` raises `TypeError: unhashable type` and aborts boot, instead of rejecting the row. Same class: project `team` as a list, member email as an int, judge `tracks` nested list, non-string title in a same-team pair, out-of-range offset timestamps (`OverflowError`).
- **Root cause:** `core/importer.py:144,184` use `in set` on unvalidated values; `except ValueError` at `:149` misses `OverflowError`.
- **Stage/module:** importer
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug3_*` (2 cases)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-4 CSV prints -0.0000 for a zero z
- **Symptom:** a project whose normalized z is exactly 0 (hand-computed) is exported as `-0.0000`.
- **Root cause:** `core/csvexport.py:20` formats float noise (-5.55e-17) with `:.4f`.
- **Stage/module:** csv export
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug4_zero_z_is_not_printed_negative`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-5 large weights collapse to zero
- **Symptom:** rubric weights 1.7e308 x3 are accepted; the sum overflows to inf, every normalized weight becomes 0, every raw_mean and z is 0.0000 and the ranking falls back to project id.
- **Root cause:** `core/scoring.py:37` sums weights without an overflow check; `/organizer/rubric` accepts any finite float.
- **Stage/module:** scoring
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug5_large_valid_weights_do_not_collapse_to_zero`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-6 malformed input returns 500
- **Symptom:** 500 on: public `GET /projects?page=99999999999999999999` (also reproduced read-only on the running container), `POST /login` / `/register` / `/set-password/*` with a non-string JSON password, `POST /organizer/assign {"k":[1]}`, `POST /organizer/judges {"tracks":[1]}`, `POST /organizer/event` and `/organizer/events` with an out-of-range offset date.
- **Root cause:** `services.py:144` OFFSET overflow (`app.py:133` has no upper bound); `services.py:35-38` calls `.encode()` on non-str; `app.py:662` catches only ValueError; `app.py:648` `.strip()` on non-str; `app.py:564,586` miss OverflowError.
- **Stage/module:** http handlers
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug6_no_500_on_malformed_input` (10 params)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-7 failed judge invite leaves an unaudited judge
- **Symptom:** the 500 from `POST /organizer/judges {"tracks":[1]}` has already inserted the user and judge rows (autocommit), with no `judge.invite` audit row.
- **Root cause:** `app.py:629-652` performs several writes without a transaction; the audit row is written last.
- **Stage/module:** http handlers / audit
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug7_failed_judge_invite_leaves_no_unaudited_judge`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-8 "invite judge" resets any existing account's password
- **Symptom:** an organizer who posts an existing user's email (a judge, a participant, another admin) to `/organizer/judges` gets a set-password link for that account; using it replaces the victim's password, so the organizer can log in as a judge and submit scores that the audit log attributes to that judge. Existing sessions stay valid.
- **Root cause:** `app.py:638-650` issues `create_password_link` for any matched user, including ones that already have a `password_hash`.
- **Stage/module:** lifecycle / auth
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug8_judge_invite_cannot_reset_an_existing_password`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-9 portal fails to boot: logger kwarg collision
- **Symptom:** first `docker compose up` exited at startup with `TypeError: stage() got multiple values for argument 'event'`; run.py reported "no response" on all 7 checks.
- **Root cause:** `logs.stage(stage_name, event, **fields)` had a positional parameter named `event`; the importer passed `event=<id>` as a field. Parameters are now positional-only.
- **Stage/module:** importer -> logging boundary (`src/dogfood/logs.py`)
- **Regression case added:** covered by `tests/golden/test_importer.py` case 1 and `test_acceptance.py` (both boot through the importer)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-10 `GET /projects/new` shadowed by `/projects/{project_id}`
- **Symptom:** found reading code before the first run: the submit form route would have been treated as a project id (404).
- **Root cause:** FastAPI matches in registration order; the parameterized route was registered first.
- **Stage/module:** HTTP routing (`src/dogfood/app.py`)
- **Regression case added:** pending (next verification pass)
- **Status:** fixed, regression case pending

## 2026-09-27 -- BUG-11 `GET /projects/new` shows the form after the deadline (Gemini review)
- **Symptom:** the form rendered after `submissions_close`; the POST was correctly refused, so nothing could be saved, but the page misled the user.
- **Root cause:** the GET handler did not ask `authz.submit_project`.
- **Stage/module:** HTTP handlers
- **Regression case added:** pending (next verification pass)
- **Status:** fixed, regression case pending

## 2026-09-27 -- BUG-12 judge can score a project of their own team (adversarial pass, code reading)
- **Symptom:** a judge who joins an assigned project's team before the close could then score it; assignment excludes conflicts but scoring did not re-check.
- **Root cause:** `authz.score_project` had no conflict-of-interest check.
- **Stage/module:** core/authz
- **Regression case added:** pending (next verification pass)
- **Status:** fixed, regression case pending

## 2026-09-27 -- BUG-13 logout with a Bearer token revoked the shared demo token
- **Symptom:** `POST /logout` with `Authorization: Bearer demo-judge-a` deleted that session for every user of the token until restart.
- **Root cause:** logout deleted whatever token authenticated the request; now it only revokes cookie sessions.
- **Stage/module:** HTTP handlers / sessions
- **Regression case added:** pending (next verification pass)
- **Status:** fixed, regression case pending

## 2026-09-27 -- BUG-14 minor contract gaps (Gemini + adversarial pass)
- **Symptom:** (a) Bearer/cookie pointing at different users was not logged (authz.md edge case); (b) reviews not counted (merged duplicates, incomplete) were dropped silently (scoring.md edge case); (c) a fixture without `event` booted an empty portal; (d) `/docs` loaded Swagger UI from a CDN (offline rule).
- **Root cause:** not implemented. Now: `auth.credential_conflict` log line; `scoring.reviews_not_counted` log line; boot raises FixtureError; docs UI disabled (`/openapi.json` remains).
- **Stage/module:** auth, scoring, boot, app
- **Regression case added:** pending (next verification pass)
- **Status:** fixed, regression case pending
