# HANDOFF (overwrite via /handoff; keep <=15 lines)
Current bead: none in progress. A 6/6, B 10/11, C 8/8 (G2 PASS), D 0/2, E 1/6, S 0/3. Effort 1285/1640 min.
Last green commit: 6361805 feat(C6) (make check 366 passed ~6 min; G2 closed after it)
Next 3 steps: 1) /next-bead E3 README (P0; copy docs/LIMITATIONS.md into "Known limitations")  2) D1 static UI (then G3)  3) B8, E1, then S*/remaining E beads
Open risks / human: pace vs 13:45 deadline (D42); make check ~370s (`slow` marker needs human OK); all 5 real adapters are mocked-HTTP only (experimental, live_tested=false) - README must say so.
Learned (not in SPEC):
- Fyers order-status vocabulary is undocumented (SDK 3.1.18 only has a history filter 1/2/3): no terminal mapping, legs end UNKNOWN; listed in docs/LIMITATIONS.md.
- AngelOne needs ANGELONE_CLIENT_LOCAL_IP/PUBLIC_IP/MAC env (create_session raises BrokerRejected naming unset vars).
- Fyers+Groww send no market-protection param (none documented); Groww maps only HTTP 401 to AuthExpired.
- Adapter pattern: ctor args with env defaults; Registry.startup swallows failed instrument loads, so tests under `respx.mock()` are safe.
- Dummy 6th broker (tests/contract/_dummy/brokers/dummy.py) is 66 code lines / 96 physical; C6 spec said <=80 LOC.
- Subagents built C2-C5 in parallel from the zerodha pattern; verify each (ruff + <=200 LOC) - ruff-format would reflow `# fmt: skip` lines, don't run `ruff format` on adapters.
