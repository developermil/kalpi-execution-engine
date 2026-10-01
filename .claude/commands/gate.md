---
description: Evaluate a decision gate with measured values (Pass / Adjust / Abort) and stop on failure
argument-hint: [G0|G1|G2|G3|G4]
---
Evaluate gate `$ARGUMENTS` (default: the ready gate from `python scripts/beads.py ready`).

1. `python scripts/beads.py show <gate>`; then read ONLY that gate's table in `docs/PLAN.md` (`grep -n "^\*\*G" docs/PLAN.md`).
2. For every row, run the command that measures it (tests, coverage, timing, `docker compose` health). Record the number, not an opinion.
3. If any check is non-trivial to judge, delegate to the `verifier` agent (fresh context) and use its verdict.
4. Print the table with measured values and a verdict per row: PASS / ADJUST / ABORT.
5. All PASS -> `python scripts/beads.py close <gate> -e "<one-line table summary>"`, run `/handoff`, and tell the human to `/clear` before the next phase.
6. Any ADJUST -> apply the "Adjust" action from the table (and only that), re-measure once, then re-evaluate.
7. Any ABORT, or ADJUST that fails twice -> STOP. Do not close the gate. Tell the human what failed, the measured value, and the recovery cascade ID from PLAN.md.
