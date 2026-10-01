# HANDOFF (overwrite via /handoff; keep <=15 lines)
Current bead: none in progress. Phase A 6/6, B 7/11 (B1-B5, B4a-c done), C 0/8. 575/1640 planned min done.
Last green commit: 62cc08d docs(B4c): D39 + B6/B7 restart-resume (make check 223 passed; engine suite 61/61 x23)
Next 3 steps: 1) /next-bead B6 (API; must start ResumeSweeper + recheck sweep in lifespan, restart test per done_when)
  2) /next-bead B7 (fault-injection, I1-I8)  3) /next-bead C0 (deferred by human until after B4b/B4c; now unblocked)
Open risks / human: elapsed ~H6:35 since first commit 15:31 -> past the H6:00 watch point (drop S*, trim E1 detail?) - ask.
  F11 overrun on B4c fake clock logged (progress.md, D39). C0 was explicitly postponed by the human; confirm order B6 -> B7 -> C0.
Learned (not in SPEC):
- Executor.run() returns RunStatus | None (None = lease not ours or lost). ExecConfig holds owner + lease_ttl_s.
- B6 needs an async factory Run -> Executor (load broker_sessions row, decrypt via TokenCipher, registry adapter) for
  ResumeSweeper(execution/resume.py); recheck sweep = Reconciler.sweep(run_id) over repo.runs_with_due_rechecks().
- Engine tests: use tests/engine/fakes.py Harness (clock.watch(sm)); never poll the DB in a wait loop (freezes fake time).
- Paper fault added: timeout_before_accept_next. Leg writes go through execution/legs.py LegWriter (fenced).
- Use the Write tool for multi-line Python source; bash heredocs with long Python were unreliable on this Windows shell.
