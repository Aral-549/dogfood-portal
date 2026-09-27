# Contract: core/confidence (prize-line confidence + tie-breaker assignment)

## Purpose
Answers "how sure is this ranking at the prize line?" for the organizer, and
proposes where one extra review would settle a close call. It reuses the
per-review normalized scores from `core/scoring` unchanged; it never alters the
published ranking. Persisting new assignments is handed to the web layer.

## Inputs
- `reviews`: list of `(judge, project, z_r, r)`: the per-review normalized score `z_r` and raw weighted score `r` exactly as `core/scoring` computes them (judge statistics are held fixed from the full data)
- `ranking`: the published `ProjectResult` list (point-estimate ranks)
- `k`: number of prize places, integer >= 1 (organizer setting, default 3)
- `seed`: integer, default 0; `replicates`: integer, default 2000

## Outputs
Per project: `{project, p_top_k, close_call, unreviewed}`
- `p_top_k`: probability in [0, 1], reported to 3 dp
- `close_call`: `0.2 <= round(p_top_k, 3) <= 0.8`, so the flag always agrees with the value shown (clarified 2026-09-27, BUG-25)
- `unreviewed`: project has zero reviews (then `p_top_k = 0`, `close_call = false`)

Plus `method: "exact" | "monte_carlo"` and `replicates` used.

## Method
1. A replicate resamples, for every reviewed project independently, `n_p` reviews with replacement from its own `n_p` reviews, and takes the mean of the drawn `z_r` (and of the drawn `r`).
2. Replicate ranking: replicate z descending; ties broken by the project's published rank (lower rank wins). Unreviewed projects are never in the top k.
3. `p_top_k` = fraction of replicates in which the project is in the top k.
4. Exact mode: if the number of distinct replicates (product over projects of `n_p ** n_p`) is <= 20 000, enumerate every equally likely draw exactly. Otherwise Monte Carlo with `replicates` draws from `random.Random(seed)`, projects processed in id order.
5. Same input, same seed: identical output.

## Behavior cases (input -> expected output)
Hand-computed. Exact mode in cases 1-5.

| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | k=1. A: z_r {+1, +1}. B: z_r {0, 0}. Published rank A=1, B=2 | A p=1.000, B p=0.000, no close calls | A's mean is always +1 > 0 |
| 2 | k=1. A: z_r {+1, -1}, published rank 1. B: z_r {0}, published rank 2 | A draws: (+1,+1) mean +1 p=1/4; mixed mean 0 p=1/2; (-1,-1) mean -1 p=1/4. A wins at +1, and at 0 by the published-rank tie-break. A p=0.750, B p=0.250. Both close calls | the tie-break rule matters |
| 3 | k=1. A: z_r {+1, -1}, published rank 2. B: z_r {0}, published rank 1 | A wins only at +1: A p=0.250, B p=0.750. Both close calls | mirror of case 2 |
| 4 | k=2. A {+1}, B {0}, C {-1}, D no reviews | A 1.000, B 1.000, C 0.000, D 0.000 `unreviewed` | single reviews never move |
| 5 | k=5, three reviewed projects | every reviewed project p=1.000 | k >= reviewed count |
| 6 | k=0 or k not an integer | ValueError | |
| 7 | fixture data, k=3, seed 0 | `method: monte_carlo`; output identical on two runs; `sum(p_top_k) = 3.000` exactly (each replicate has exactly 3 winners) | invariant check, not a snapshot |

## Tie-breaker assignment (organizer action)
Input: close-call projects, current assignments, judge tracks, conflicts (as in `core/assignment`).
For each close-call project, ordered by `|p_top_k - 0.5|` ascending, then project id: add ONE judge who is not already assigned and not conflicted. Candidates cover the project's track (all judges if none do). Preference order:
(a) most reviews on OTHER close-call projects (comparability), (b) lowest current load, (c) judge id.

| # | Input | Expected output |
|---|-------|-----------------|
| 8 | close calls P (p=0.5), Q (p=0.3), both in track t. J1 covers t, has reviewed (so is assigned to) Q, load 1. J2 covers t, load 0, no reviews. Nobody else covers t | P first (closer to 0.5): candidates J1, J2; J1 wins on preference (a), 1 review on another close call vs 0, despite higher load. Then Q: J1 is already assigned to Q, so Q gets J2. Result: (J1, P), (J2, Q) |
| 9 | close call whose every candidate is assigned or conflicted | no assignment for it; reported as `no_candidate` |
| 10 | run twice with no new scores | second run adds nothing new for projects that already received a tie-breaker judge | idempotent via existing assignments |

## Web surface
- Organizer dashboard: a `P(top k)` column, close-call flag, `k` setting, "Assign tie-breaker judges" button (audited: `assignment.tiebreak`, with the pairs added).
- CSV: a separate organizer export `/api/confidence.csv` with columns `rank,project_id,title,p_top_k,close_call,unreviewed`. (Amended 2026-09-27: the results CSV header is frozen by `csv-export.md` case 1 and its golden tests, so no columns are added there.)
- Authz: organizer only, same as `export_results`. Judges and participants never see `p_top_k`.

## Edge cases that must be covered
- A project whose reviews are all from uninformative judges (all z_r = 0) is decided purely by tie-breaks; still valid.
- Merged duplicates: uses the same effective reviews as scoring (prj_41 with 6).
- Float noise: replicate means rounded to 12 dp before comparison, as in scoring.

## Explicitly out of scope
- Changing the published ranking (confidence is advisory only).
- Recomputing judge statistics per replicate (documented as a limitation in JUDGING.md).

## Status
- [x] Drafted
- [x] Reviewed by a human (approved 2026-09-27)
- [ ] Implementation matches this contract
- [ ] Golden tests exist for every behavior case above
