# progress.md â€” append-only; `beads.py close` writes lines here automatically
# Format: - <timestamp> | <bead> done | <measured evidence>

- 2026-10-01 15:51+05:30 | A1 done | 5/5 docs/brokers/*.md; BROKERS.md 13x5 table, 0 empty, every cell URL or UNVERIFIED; UNVERIFIED cells=12/65 (zerodha 1, upstox 0, fyers 6, angelone 2, groww 3); +30 cells with partial (dagger) UNVERIFIED sub-items
- 2026-10-01 19:45+05:30 | A2 done | compose up --build: /healthz 200 in 2s after start, app runs as non-root uid 'app', db healthy; make check exit 0 (ruff, mypy domain, 2 tests pass); Dockerfile pins --workers 1
- 2026-10-01 19:54+05:30 | A3 done | tests/unit/test_domain.py 50 passed (tags 8-20 alnum deterministic; 9 invalid-payload cases rejected; 14 run_status table rows; sell_failed over all 11 LegStatus); mypy strict domain/ clean (7 files); make check exit 0
- 2026-10-01 20:02+05:30 | A4 done | 39 new tests pass: GET /v1/brokers lists paper with experimental/live_tested/market_order_verified; drop-in file test registers 'dropin' with no other edits; 24 classify cases cover every SPEC §4 row; 10 Paper tests (place/get/find_by_tag + all fault hooks). make check exit 0 (93 tests); mypy strict clean on brokers/
