# HANDOFF (overwrite via /handoff; keep <=15 lines)
Current bead: none in progress. A 6/6, B 10/11, C 1/8 (C0 done), E 1/6 (E2 done). Ready: C1, C2 (P0), B8, D1, E1.
Last green commit: 96ff599 feat(C0) (make check 295 passed ~4.5 min; make itest 3 passed ~5s + build)
Next 3 steps: 1) /next-bead C1 Zerodha  2) /next-bead C2 Upstox  3) continue C3-C5 (one file + one fixture each)
Open risks / human: pace vs 13:45 deadline (D42); make check ~270s (B7 chaos ~104s) - `slow` marker needs human OK.
Learned (not in SPEC):
- New broker = brokers/<id>.py + tests/contract/fixtures/<id>.py with hooks listed in tests/contract/harness.py
  docstring. Template: tests/contract/_dummy/{brokers,fixtures}/dummy.py (HttpBrokerAdapter + respx + InstrumentResolver).
- Adapter instrument master: build `InstrumentResolver(fetch, parse)` in __init__, load it in `startup()` (Registry.startup
  runs at lifespan, failures logged; resolve() loads lazily on miss, failed load not cached).
- respx: re-registering the same route pattern replaces its response (tag_hit then tag_miss on GET /orders works).
- make itest uses compose project `kalpi-itest` + docker-compose.itest.yml (test-only keys), down -v before/after;
  app log tail in .itest-app.log. E2 asserts healthy runs have no `run.resumed` (stall detector; D44).
- ruff isort puts `tests.*` imports as third-party in files under tests/contract/_dummy (harmless).
