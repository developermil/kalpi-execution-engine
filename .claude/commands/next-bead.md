---
description: Take the next unblocked bead (or the given id), build it to its done-when, verify, close, commit
argument-hint: [bead-id]
---
Work exactly one bead.

1. Pick: if `$ARGUMENTS` is set use it, else run `python scripts/beads.py ready` and take the first P0 line. If the id is a gate (G*), run `/gate` instead.
2. `python scripts/beads.py show <id>` — read nothing else except the SPEC/DECISIONS section it names (grep headings, then read only that range).
3. `python scripts/beads.py start <id>`.
4. Write the failing test(s) from `done_when` first when the bead is code; then implement the smallest thing that satisfies them. Stay inside the bead's scope.
5. Verify with a command that proves `done_when` (tests, curl, script). Use `-q -x --tb=short` and tail output. If it fails: fix; after 3 failed attempts stop, split the bead (add new beads to docs/beads.toml, run `beads.py lint`), log in progress.md, and ask the human (failure F11).
6. `make check` must be green.
7. `python scripts/beads.py close <id> -e "<measured result, e.g. '27 tests pass, coverage 96%'>"`, then `git add -A && git commit -m "feat(<id>): <summary>"`.
8. Print ONE line: bead id, evidence, next ready bead. Do not recap the work.
