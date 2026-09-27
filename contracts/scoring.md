# Contract: core/scoring (weighted rubric + cross-judge normalization)

## Purpose
Turns raw review scores into a ranked result per project: raw weighted mean,
normalized score, rank and a confidence flag. Pure function over data passed
in; no database or clock access. Presentation (CSV, dashboard) is handed off.

## Inputs
- `criteria`: `{name: weight}`, weights are numbers > 0 (need not sum to 1)
- `reviews`: list of `{judge, project, criteria: {name: int 1..5}}`, already validated by the importer and duplicate-merged by `effective_reviews`
- `projects`: list of canonical project ids (projects may have zero reviews)

## Outputs
Per project: `{project, n_reviews, raw_mean, z, rank, raw_rank, low_confidence}`

`raw_rank` is the rank by `raw_mean` alone (same tie-breaks), so the dashboard can show
the ranking change normalization causes (Normalization Proof bonus).

## Method
1. Weights normalized: `w_c = weight_c / sum(weights)`.
2. Per review, raw weighted score `r = sum(w_c * x_c)`.
3. `raw_mean` per project = mean of its `r`.
4. For each judge J and criterion c, over all of J's reviews: `mu_Jc`, `sd_Jc` (population sd).
   Judge J is *informative on c* iff J has >= 2 reviews and `sd_Jc > 0`.
5. Per review, per criterion: `z_c = (x_c - mu_Jc) / sd_Jc` if informative, else `0`.
6. Per review `z_r = sum(w_c * z_c)`. Per project `z` = mean of `z_r` over its reviews.
7. `rank`: sort by `z` desc, then `raw_mean` desc, then project id asc. Ranks are 1..N, no ties.
8. `low_confidence = true` if the project has fewer than 2 reviews from judges informative on at least one criterion.
9. Projects with zero reviews: `n_reviews=0, raw_mean=null, z=null`, ranked after all reviewed projects (by id), `low_confidence=true`.

Rationale: z-scoring per judge removes each judge's harshness/leniency and scale use.
A judge who gave everything the same score (jdg_07: 4,4,4 on 3 projects) carries no
ranking information, so contributes 0 rather than inflating or dividing by zero.
A judge with one review (jdg_01, jdg_23) likewise contributes 0.

## Behavior cases (input -> expected output)
Hand-computed. Equal weights unless stated. Values to 4 dp.

| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | one criterion `q:1`. Judge A: P1=2, P2=4. Judge B: P1=5, P2=5 | A: mu 3, sd 1 -> P1 z=-1, P2 z=+1. B: sd 0 -> 0. P1 z=-0.5, P2 z=+0.5. raw_mean P1=3.5, P2=4.5. rank P2=1, P1=2 | constant judge contributes 0 |
| 2 | `q:1`. Judge A: P1=1, P2=3 (harsh). Judge B: P1=3, P2=5 (lenient) | both judges give P1 z=-1, P2 z=+1. P1 z=-1, P2 z=+1 | offsets removed |
| 3 | `q:1`. Judge A only: P1=4 (single review) | P1 z=0, raw_mean 4, low_confidence true | n=1 judge |
| 4 | weights `f:3, q:1`. Judge A: P1 f=5 q=1, P2 f=1 q=5 | w_f=0.75, w_q=0.25. raw P1=4.0, P2=2.0. z: P1 f=+1 q=-1 -> 0.5; P2 f=-1 q=+1 -> -0.5. rank P1=1 | weights applied to both raw and z |
| 5 | case 1 plus P3 with zero reviews | P3: n=0, raw_mean null, z null, rank 3, low_confidence true | |
| 6 | two projects with identical z and raw_mean | lower project id ranks first | deterministic |
| 7 | weights `{}` or any weight <= 0 | ValueError, no partial result | |
| 8 | upstream fixtures, equal weights | 40 canonical projects ranked 1..40; jdg_07, jdg_01, jdg_23 contribute 0 everywhere | golden snapshot of the full ranking to be hand-checked on 3 projects before freezing |

## Edge cases that must be covered
- Judge informative on one criterion but constant on another (sd 0 on that criterion only).
- Floating point: z values compared with tolerance 1e-9 in tests; rank must not flip on float noise (round z to 12 dp before sorting).
- A review whose project is not in `projects` (superseded duplicate): ignored, and counted in a warning.
- Criteria in reviews but not in `criteria` config: ignored; config criterion missing from a review: that review is rejected upstream (importer), so scoring may assume completeness and asserts it.

## Explicitly out of scope
- Pairwise / Bradley-Terry (bonus, later).
- Deciding who judges what (assignment contract).

## Status
- [x] Drafted
- [ ] Reviewed by a human
- [ ] Implementation matches this contract
- [ ] Golden tests exist for every behavior case above
