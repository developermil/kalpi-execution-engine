---
description: Write a <=15 line handoff so the next session (or post-compaction) resumes cheaply
---
Overwrite `docs/HANDOFF.md` with at most 15 lines:
- Current bead and its status (from `python scripts/beads.py status`)
- Last green commit (`git log -1 --oneline`)
- Next 3 concrete steps
- Open risks / decisions pending the human
- Anything learned that is NOT in SPEC/DECISIONS (append a row to DECISIONS.md instead if it is a decision)

Then commit (`docs: handoff`) and tell the human they can `/clear` and run `/resume`. Print nothing else.
