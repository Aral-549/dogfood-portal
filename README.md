# DOGFOOD portal

A self-hosted hackathon submission and judging platform. It covers registration,
teams, submissions with a hard deadline, a public gallery, judge assignment,
weighted rubric scoring, documented cross-judge normalization, results and CSV
export. Role isolation is enforced in the backend.

Built for [DOGFOOD 2026](https://dogfoodhack.com). MIT licensed.

## Run it

```
docker compose up
```

Open http://localhost:8080. The portal seeds itself from `fixtures.json` on
first boot and prints demo logins and API headers to the log:

| Role        | Login                          | Password       | API header                              |
|-------------|--------------------------------|----------------|-----------------------------------------|
| Organizer   | organizer@dogfood.local        | dogfood-demo   | `Authorization: Bearer demo-organizer`  |
| Judge A     | diego.herrera@example.org (jdg_24) | dogfood-demo | `Authorization: Bearer demo-judge-a` |
| Judge B     | jonas.vogel@example.org (jdg_26)   | dogfood-demo | `Authorization: Bearer demo-judge-b` |
| Participant | priya1@example.org (team tm_01)    | dogfood-demo | `Authorization: Bearer demo-participant` |

The data lives in the `dogfood-data` volume. `docker compose down -v` resets it.
After the image is built once, it runs with the network off: no cloud accounts,
hosted database or external APIs.

**For a real event**, set `DOGFOOD_DEMO_SESSIONS: "0"` in `docker-compose.yml`.
The demo tokens are then deleted at boot.

## Acceptance check

One command, from a clean, freshly seeded portal:

```
./scripts/check.sh
```

It starts its own isolated copy of the portal (separate compose project and
volume, so your data is never touched), waits until it is healthy, runs the
official `run.py` unmodified, writes `acceptance-report.txt`, and exits
non-zero if T1 or T2 is not verified. We claim all four tiers; `run.py` only has checks
for T1 and T2 (T3 and T4 are judged by hand, per the brief), so the report ends with
`note: claimed but not verified: T3 T4`. That line is expected. Stop your own portal first, since
both use port 8080. If you use an HTTP proxy, the script already exempts
localhost; for the plain `run.py` form, set `no_proxy=localhost,127.0.0.1`
first. The same script runs in GitHub Actions on every push
(`.github/workflows/acceptance.yml`).

Against an already running portal, the plain form also works:

```
python3 run.py .dogfood.toml > acceptance-report.txt
```

The committed `acceptance-report.txt` is the unedited output of
`./scripts/check.sh`.

## One event, end to end

1. **Organizer** (`/organizer`): set dates and prizes, set rubric weights,
   invite judges (you get a set-password link to pass on), run auto-assign.
   Admins can create new events.
2. **Participant**: register, create a team (you get an invite link), teammates
   join via `/join/<code>`, submit a project (drafts allowed) and edit it
   until the deadline.
3. **Judge** (`/judge`): score each assigned project 1-5 per criterion, with a
   comment, until judging closes.
4. **Organizer**: watch progress and the raw vs normalized ranking, check
   low-confidence and duplicate flags, publish results, download the CSV.
5. **Public**: `/projects` gallery with search and track filter; `/results`
   after publishing.

The fixture event closed on 2026-03-01, so its submissions are frozen, and
the checker depends on that: moving its deadline into the future makes the
"closed event refuses submissions" check fail, and T1 with it. To demo the
submission flow anyway, move the deadline on the organizer page, then run
`docker compose down -v` afterwards to restore the fixture data.
`./scripts/check.sh` is unaffected either way because it uses its own volume.

## Beyond T2: public participation and integrations

**Community vote (T3).** The organizer sets a voting window on the organizer
page. Logged-in users vote at `/vote`; ballots are in a random order unique to
each voter; nobody sees tallies (organizers included) until the window
closes. Comments on project pages. Rate limits, duplicate-account flags and a
full audit trail; the organizer can void a flagged account's votes.
Failed logins are throttled per email and client IP, so a stranger's guesses
cannot lock the real owner out. Behind a reverse proxy, set
`FORWARDED_ALLOW_IPS=<proxy ip>` so uvicorn sees real client IPs (otherwise every
visitor looks like the proxy). `DOGFOOD_RATE_LIMITS=off` is for load tests only.

**API.** Everything is under `/api/v1`, documented at `/openapi.json`. Create a
personal token with `POST /api/v1/tokens` and send it as
`Authorization: Bearer <token>`; it carries your own roles, nothing more. The whole account
lifecycle works without a browser too: `POST /api/v1/register`, `/api/v1/login` (returns a
14-day bearer session), `/api/v1/logout`, `/api/v1/set-password/{token}`. Every form action in
the UI has an `/api/v1` twin.

**Webhooks.** Organizer page, "Add webhook". Each delivery is signed:
`X-Dogfood-Signature: sha256=<HMAC-SHA256(secret, raw body)>`. Failed deliveries
are retried after 10 s, 60 s and 300 s. Nothing leaves the portal until you add
a webhook.

**Certificates and signed judge records.** After publishing results, "Issue
signed certificates and judge records". Verifying a record offline, without
trusting the portal:

```
curl -s http://localhost:8080/records/<id>.json > rec.json
curl -s http://localhost:8080/.well-known/dogfood-signing-key.pem > key.pem
python3 -c "import json,base64;d=json.load(open('rec.json'));open('canon.json','wb').write(json.dumps(d['record'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode());open('sig.bin','wb').write(base64.b64decode(d['signature']))"
openssl pkeyutl -verify -pubin -inkey key.pem -rawin -in canon.json -sigfile sig.bin
```

Back up `/data/signing_key` with the volume: a new key cannot verify old records.

**Embed the gallery** on any site:
`<script src="http://localhost:8080/widget.js" data-event="evt_01"></script>`

**Moving an event in and out.** `GET /api/v1/events/<id>/export.json` (organizer)
gives the whole event in the `fixtures.json` shape plus rubric, exclusions,
comments and (after voting closes) votes. To load it into a fresh portal:

```
DOGFOOD_FIXTURES=none docker compose up -d
docker compose exec portal python -m dogfood.cli create-admin you@example.org
# open the printed set-password link, log in, create a token, then:
curl -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
     --data-binary @evt_01.json http://localhost:8080/api/v1/import
```

## Beyond the brief: ideas from other platforms

`RESEARCH.md` compares Devpost, Gavel, MLH's judging guide, DoraHacks and others, and
explains what we adopted:

- **Order rank**: a Bradley-Terry ranking from each judge's ordering of their projects,
  immune to judge generosity, beside the published and shrunk ranks; projects whose top-k
  place depends on the method are flagged *prize line disputed*
  (`GET /api/v1/results/cross-check`).
- **Judge recusal**: judges declare a conflict of interest; the pairing is removed everywhere.
- **Integrity checks before announcing**: shared repositories and near-identical submissions
  across teams, missing repositories, prize contenders first (`GET /api/v1/integrity`).
- **Judging plan**: MLH's sizing formula, per-judge load, under-reviewed projects.
- **Top of each track** on the results page.

## Docs

- `ARCHITECTURE.md`: how it fits together and why
- `DATA-MODEL.md`: schema, import and export
- `JUDGING.md`: assignment, scoring maths, normalization, and its limits
- `contracts/`: the input -> expected-output specs the code was built against
- `RESEARCH.md`: comparable platforms, what we adopted and what we rejected
- `THREAT-MODEL.md`: sybil votes, ballot stuffing, judge collusion, deadline gaming and more:
  what we stop, what we do not

## Honest limits

- No email delivery. Invite and set-password links are shown to the
  organizer to pass on, which keeps the portal offline-capable.
- Several events: the header has an event switcher, and the browser remembers
  the last event picked. The API never guesses: pass `?event=<id>` (default: the
  first event).
- Normalization uses plain per-judge z-scores, with no shrinkage for judges
  who scored few projects. See `JUDGING.md`, "Known limits".
- Eligibility is "submitted, not superseded, before the deadline" plus an organizer's
  audited ruling (Organizer page, "Eligibility"); there are no automatic per-track rules.
- No password reset, and no rate limiting on login. The 91 fixture participants have no
  password; only the demo participant can log in. (Judges get a set-password link
  from the organizer; it only works for accounts that have no password yet and are
  not on a team, so an organizer can never claim a participant's account.)
- `/docs` (Swagger UI) is disabled because it loads from a CDN; the schema is at `/openapi.json`.
- T3 (public voting) and T4 (API/webhooks/certificates) are not implemented.
