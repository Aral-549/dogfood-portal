# Contract: core/agreement (judge agreement, outlier and favoritism report)

## Purpose
Tells the organizer which judges disagree with everyone else, which single
reviews look like favoritism, and which judges carry no information, using
the normalized per-review scores from `core/scoring`. Lets the organizer
exclude a judge's reviews, audited and publicly disclosed. It never excludes
anyone automatically.

## Inputs
- `reviews`: list of `(judge, project, z_r)` as computed by `core/scoring` (effective reviews, duplicates merged)
- `informative`: set of judges informative on at least one criterion (from scoring)

## Outputs
Per judge: `{judge, shared, agreement, mean_abs_dev, status}`
- `shared`: number of the judge's projects that have at least one OTHER review
- `consensus` for a review of project P by J = mean `z_r` of the other reviews of P (leave-one-out)
- `agreement`: Pearson correlation between J's `z_r` and consensus over shared projects, 3 dp, or null
- `mean_abs_dev`: mean `|z_r - consensus|` over shared projects, 3 dp, or null
- `status`: `uninformative` | `insufficient` (shared < 4 or zero variance on either side) | `outlier` (agreement < -0.3) | `ok`

Thresholds amended 2026-09-27 (human-approved): were shared < 3 and agreement < 0, which flagged 7 of 30 fixture judges, one at -0.005.

Per review flag `favoritism` when `z_r - consensus >= 2.0` (J rates P far above its other judges).

## Behavior cases (input -> expected output)
| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | J on P1..P4 with z_r (-1, 0, 0, +1); consensus from others (-0.5, 0, 0, +0.5) | agreement 1.000, mean_abs_dev 0.250, status ok | consensus = J / 2, perfectly linear; (0.5 + 0 + 0 + 0.5) / 4 |
| 2 | J z_r (+1, 0, 0, -1); consensus (-1, 0, 0, +1) | agreement -1.000, mean_abs_dev 1.000, status outlier | (2 + 0 + 0 + 2) / 4 |
| 3 | J shares only 3 projects | agreement null, status insufficient | below the 4-project minimum |
| 3a | J over 4 shared projects with agreement between -0.3 and 0 | status ok, agreement reported | weak disagreement is not flagged |
| 4 | J's review of P: z_r +1.5, other reviews of P: -0.5 and -1.1 (consensus -0.8) | that review flagged favoritism (diff 2.3) | per review, independent of status |
| 5 | J not in `informative` (e.g. fixture jdg_07: 4,4,4 everywhere) | status uninformative, no agreement computed | |
| 6 | J's projects have no other reviewers | shared 0, status insufficient | |
| 7 | consensus constant across J's shared projects | agreement null (zero variance), status insufficient | no division by zero |

## Exclusion (organizer action)
| # | Input | Expected output |
|---|-------|-----------------|
| 8 | organizer excludes judge J with a non-empty reason | J's reviews are left out of scoring, confidence and CSV; audit row `judge.exclude` with the reason; dashboard lists the exclusion |
| 9 | exclusion with empty reason | 422 |
| 10 | organizer re-includes J | results return exactly to the pre-exclusion state; audit row `judge.include` |
| 11 | public `/results` after publish with any exclusion active | shows "Reviews from N judge(s) were excluded by the organizer" (count only, no names or reasons) |
| 12 | judge, participant or visitor requests the report or exclusion | 403 / 403 / 401 |

Excluded reviews are never deleted: exclusion is a flag on the judge for that event, so it is reversible and auditable.

## Edge cases that must be covered
- Consensus uses normalized `z_r`, never raw scores, so harsh-but-consistent judges are not flagged.
- Exclusion changes judge statistics for nobody else (other judges' mu/sd come from their own reviews only).
- Excluding every judge of a project leaves it with zero reviews, ranked last with `low_confidence`.

## Explicitly out of scope
- Automatic exclusion, and any penalty to a judge beyond the organizer's decision.
- Detecting collusion between two judges (pairwise); documented as future work in JUDGING.md.

## Status
- [x] Drafted
- [x] Reviewed by a human (approved 2026-09-27)
- [ ] Implementation matches this contract
- [ ] Golden tests exist for every behavior case above
