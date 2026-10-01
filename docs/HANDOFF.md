# HANDOFF (overwrite via /handoff; keep <=15 lines)
Current bead: none in progress. A,C,D,E done (E 6/6), B 10/11 (B8 open), S 0/3. G4 PASS: repo public https://github.com/developermil/kalpi-execution-engine. Effort 1500/1640 min.
Last green commit: 3081e11 (make check 392 passed ~4 min; make itest 3 passed with APP_PORT=18002; unauth clone healthy 26s, README demo COMPLETED)
Next 3 steps: 1) human: final read of README/Limitations + submit (R12 24h is the submitter's call)  2) optional P1 B8 adaptive concurrency halving (README lists it as not built; remove that line if built)  3) optional P2 S1-S3 (stretch)
Open risks / human: all 5 real adapters are mocked-HTTP only (experimental, live_tested=false); Fyers order status codes undocumented (legs never FILLED); owner's compose stack must be left alone (project `kalpi-engine`, :8000).
Learned (not in SPEC):
- Compose project = directory name: a same-named clone shares containers/volume. Always `APP_PORT=<free> docker compose -p <name> ...`; never `down -v` on `kalpi-engine`. itest/test_compose.py honours APP_PORT.
- UI: frontend/index.html + frontend/portfolio.js (.json/.csv upload parser, unit-tested under node in tests/unit/test_ui_upload.py); GET /ui, /ui/portfolio.js; UI default API key is change-me-demo-key.
- Logging is stdlib JSON + redaction (D45, kalpi_engine/logging.py); /readyz = DB + non-empty registry (503 otherwise).
- scripts/demo.sh = README commands verbatim; scripts/ui_smoke.py drives UI flows + docs/samples and writes docs/img/ (needs local Chrome for screenshots, node for CSV parse).
- Headless-Chrome DOM check of the upload UI works via DataTransfer; virtual-time-budget races File.text(), so poll instead of fixed timeouts.
- Subagents (parallel adapters, audits) must be told not to touch the owner's stack or .env.
