# HANDOFF (overwrite via /handoff; keep <=15 lines)
Current bead: none in progress. A 6/6, B 9/11 (B7 + gate G1 PASS), C 0/8. Ready: E2 (P0), B8, D1, E1.
Last green commit: 1a95894 docs(G1) (code: 407093e test(B7), make check 258 passed, ~2.5 min)
Next 3 steps (order D43): 1) /next-bead E2 (compose + Postgres)  2) /next-bead C0  3) continue C* adapters
Open risks / human: pace vs 13:45 deadline (D42). make check is now ~156s; the B7 chaos suite takes ~104s
  (20 seeds x 100 legs) - if it hurts, consider a `slow` marker (needs human OK; do not drop seeds silently).
Learned (not in SPEC):
- Chaos harness: tests/engine/test_faults.py ChaosPaper(PaperBroker) assigns faults per tag from seeded RNG
  (reproducible despite concurrency); I3 probed in place_order by reading legs from DB; PRICES monkeypatched.
- I8 randomised: tests/unit/test_restart_faults.py strands runs via `update(Leg).where(Leg.tag == ...)`;
  OrderIntent.leg_id is NOT the DB Leg.id (matching on it silently updates 0 rows).
- `pytest -p no:logging` removes caplog -> test_storage errors at setup; not a real failure.
- G1 I5 measured by scanning events/outbox/DB bytes/DEBUG logs for the Paper token on seeds 0,7,19: absent.
