---
description: Fresh-context requirements audit (prompts vs beads before freeze; R1-R12 vs repo before release)
argument-hint: [plan|release]
---
Mode `$ARGUMENTS` (default `plan` if no code exists yet, else `release`).

**plan**: delegate to the `plan-critic` agent. Also check that every prompt in `docs/PROMPTS.md` maps to at least one bead or decision. Output: gaps list. Record adopted/rejected critiques in the review-log table at the bottom of `docs/PLAN.md` with a reason for every rejection. Then run `python scripts/beads.py lint`.

**release**: delegate to the `verifier` agent with this brief: "Audit the repo against R1-R12 in docs/ASSIGNMENT.md. For each: OK / WEAK / MISSING with file:line evidence. Run `make check`. Try the README setup steps literally." Fix every MISSING; list every WEAK under README "Limitations" unless fixed. Fill the "Where satisfied" column in docs/ASSIGNMENT.md.
