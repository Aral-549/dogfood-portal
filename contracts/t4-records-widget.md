# Contract: T4 certificates, signed judge records, embeddable gallery widget

## Purpose
Certificates for participants and judges, and signed judge participation
records that anyone can verify without trusting (or even reaching) the portal;
plus a gallery widget other sites can embed. One signing mechanism serves both
certificates and records.

## Inputs
- Signing key: Ed25519, generated on first boot into `/data/signing_key` (0600), never exported
- A record is issued by the organizer after results are published

## Outputs
- `GET /.well-known/dogfood-signing-key.pem`: the public key (PEM)
- `GET /records/{id}`: human-readable certificate page (print-friendly), shows a verification link
- `GET /records/{id}.json`: `{"record": {...}, "signature": "<base64 Ed25519 over the canonical JSON of record>"}`
- Canonical JSON: `json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`
- `GET /verify/{id}`: page that checks the signature server-side and says valid / invalid
- `GET /embed/gallery?event=<id>`: minimal gallery page for iframes; `GET /widget.js`: one-line embed script

## Record contents
Participant certificate: `{type: "participant", record_id, event_id, event_name, name, team, project, project_title, issued_at}`.
Judge record: `{type: "judge", record_id, event_id, event_name, judge_id, name, projects_reviewed (int), criteria, issued_at}`. Never includes score values or which projects were reviewed (peer isolation).

## Behavior cases (input -> expected output)
| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | organizer issues records after publishing | one participant certificate per member of each canonical submitted project's team, one judge record per judge with >= 1 review; audit `records.issue` | fixture event: 30 judges have reviews |
| 2 | issue before publishing results | 409 `results_not_published` | |
| 3 | issue twice | no duplicates (idempotent per person and event) | |
| 4 | verify `/records/{id}.json` with `openssl pkeyutl -verify -pubin -inkey key.pem -rawin -in canonical.json -sigfile sig.bin` | "Signature Verified Successfully" | offline, portal not needed; documented in README |
| 5 | change any byte of the record and verify | fails | tamper evidence |
| 6 | `/verify/{id}` for a valid record / unknown id | "valid" / 404 | |
| 7 | judge record for jdg_24 | `projects_reviewed = 11`; no score values, no project ids | peer isolation |
| 8 | records and certificates are public URLs | yes, but `record_id` is 128 random bits (unguessable) and only listed to the person themselves (`/me`) and to organizers | |
| 9 | signing key file missing on a later boot | new key generated; old records then fail verification, and boot logs a loud warning | key must be backed up with the volume |
| 10 | `/embed/gallery` | 200, same projects as `/projects` page one; header `Content-Security-Policy: frame-ancestors *` | embeddable |
| 11 | every other HTML page | `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'` | clickjacking protection elsewhere |
| 12 | `/widget.js` | JS that inserts an iframe to `/embed/gallery` sized to its container; no other network calls, no cookies read | |
| 13 | embed page | contains no scores, judges, votes, emails; loads no external resources | |

## Edge cases that must be covered
- Names with non-ASCII characters sign and verify (UTF-8 canonical JSON).
- A certificate page prints to one A4 page with the browser's print dialog (no PDF library).
- The public key endpoint works with the network off.

## Explicitly out of scope
- Key rotation with a published key history (documented as future work).
- PDF generation server-side.

## Status
- [x] Drafted
- [x] Reviewed by a human (approved 2026-09-27)
- [x] Implementation matches this contract
- [x] Golden tests exist for every behavior case above
