# HANDOFF (overwrite via /handoff; keep <=15 lines)
Current bead: none in progress. Phase A 6/6, B 8/11 (B6 done), C 0/8. 635/1640 planned min done.
Last green commit: 72afcd1 docs D40-D43 (code: 4cb8919 feat(B6), make check 233 passed)
Next 3 steps (order D43, encoded in beads deps): 1) /next-bead B7 (fault-injection I1-I8)
  2) /gate G1  3) /next-bead E2 (compose + Postgres), then C0
Open risks / human: pace judged vs 13:45 deadline (D42), not H-hours. S* kept (blocked behind G4).
Learned (not in SPEC):
- Service layer = src/kalpi_engine/service.py Runtime: load_session (token+extra encrypted as one JSON blob),
  make_executor/make_reconciler, launch(run), recheck_forever, resume_sweeper(); lifespan in main.py starts
  notifier + resume sweep + recheck loop; app.state.runtime. Settings gained poll_interval_s, recheck_interval_s.
- API tests: tests/unit/test_api.py (TestClient, real time, poll_interval 0.05); _stranded_run() helper builds
  crashed/UNKNOWN runs; share one Registry across app instances so Paper accounts survive a "restart".
- Error envelope + handlers in api/errors.py (ApiError); 422 multi-error code = VALIDATION_FAILED, schema = VALIDATION_ERROR.
- README must include docs/LIMITATIONS.md items (E3 `what` updated).
