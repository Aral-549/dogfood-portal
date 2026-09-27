# Bug Log

Every entry here must result in a permanent case added to `tests/golden/`
before it's marked resolved. A patched bug without a regression case is not
resolved — it's just hidden until the next rewrite.

---

## 2026-09-27 — Portal fails to boot: logger kwarg collision
- **Symptom:** `docker compose up` exits at startup with `TypeError: stage() got multiple values for argument 'event'`; checker reports "no response" on all 7 checks.
- **Root cause:** `logs.stage(stage_name, event, **fields)` had a positional parameter named `event`; the importer passed `event=<id>` as a field.
- **Stage/module:** importer -> logging boundary (`src/dogfood/logs.py`, `src/dogfood/core/importer.py`)
- **Regression case added:** pending: `tests/golden/` import-on-empty-db case (fixtures-import.md case 1) covers the boot path
- **Status:** fixed, regression case pending

## 2026-09-27 — `GET /projects/new` shadowed by `/projects/{project_id}`
- **Symptom:** found in code review before the first run: the submit form route would have been treated as a project id and returned 404.
- **Root cause:** FastAPI matches routes in registration order; the parameterized route was registered first.
- **Stage/module:** HTTP routing (`src/dogfood/app.py`)
- **Regression case added:** pending: `GET /projects/new` returns the form (200), not 404
- **Status:** fixed, regression case pending
