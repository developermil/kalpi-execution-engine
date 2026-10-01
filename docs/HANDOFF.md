# HANDOFF (overwrite via /handoff; keep <=15 lines)
Current bead: none in progress. Phase A done (A1-A5); G0 closed ADJUST (D37). 220/1640 planned min done.
Last green commit: c74d7b7 docs(G0): gate ADJUST (make check green, 93 tests)
Next 3 steps: 1) /next-bead B1 (planner V1-V11, builds on domain/schemas.py ExecuteRequest)
  2) /next-bead B2 (persistence)  3) /next-bead C0 (contract suite; adapters subclass brokers/base.py HttpBrokerAdapter)
Open risks / human: elapsed H4:37 at G0 (2.5h idle 17:04-19:36) - D1 already cut to minimal UI; watch H6:00 (drop S*, E1 detail).
  All 5 real brokers ship experimental=true unless a live call succeeds (F1 step 3, enforced in C1-C5).
Learned (not in SPEC):
- base.py extras beyond SPEC §4: Outcome.OK, hooks error_code()/order_id_from(), request() = send+classify+raise, aclose().
- Payload-only checks (V6, V7, FIRST_TIME all-BUY, qty>0 strict int) already raise in domain/schemas.py; B1 adds holdings/broker rules and must collect ALL violations.
- Paper: create_session({holdings:{SYM:qty}|profile:'demo', cash}); Faults dataclass on broker.faults; no tag dedupe; sellable/cash blocked at placement.
- GET /v1/brokers is unauthenticated for now (API-key auth comes with sessions/executions bead).
- Heredocs with long Python in Bash failed once on this Windows shell; use Write tool for source files.
