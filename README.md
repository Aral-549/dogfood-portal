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

```
python3 run.py .dogfood.toml > acceptance-report.txt
```

The committed `acceptance-report.txt` is the unedited output against
`docker compose up`.

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

The fixture event closed on 2026-03-01, so its submissions are frozen (the
checker depends on that). To demo the submission flow, move its
`submissions_close` into the future from the organizer page, or create a new
event.

## Docs

- `ARCHITECTURE.md`: how it fits together and why
- `DATA-MODEL.md`: schema, import and export
- `JUDGING.md`: assignment, scoring maths, normalization, and its limits
- `contracts/`: the input -> expected-output specs the code was built against

## Honest limits

- No email delivery. Invite and set-password links are shown to the
  organizer to pass on, which keeps the portal offline-capable.
- One event is the default view. Other events are selected with `?event=<id>`;
  there is no event switcher in the UI yet.
- Normalization uses plain per-judge z-scores, with no shrinkage for judges
  who scored few projects. See `JUDGING.md`, "Known limits".
- No eligibility rules beyond "submitted, not superseded, before the deadline".
- No password reset for participants, and no rate limiting on login.
- T3 (public voting) and T4 (API/webhooks/certificates) are not implemented.
