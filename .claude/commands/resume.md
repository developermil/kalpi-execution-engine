---
description: Restore working context after /clear or compaction in ~2k tokens (never trust the compaction summary over files)
---
1. `python scripts/beads.py status` and `python scripts/beads.py ready`
2. Read `docs/HANDOFF.md` and `git log -5 --oneline` and `tail -5 docs/progress.md`
3. If a bead is `in_progress`: `python scripts/beads.py show <id>` and `git status --short`
4. Say in two lines where we are and what you will do next, then continue with `/next-bead`.
Do not re-read SPEC/PLAN/DECISIONS in full.
