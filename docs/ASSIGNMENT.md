# Assignment: Problem Statement 1 — Portfolio Trade Execution Engine

Source: Kalpi Builder Assignment v2 (Backend/Infra Engineer). Time allowance: 24h from receipt.
Questions to Kalpi: info@kalpicapital.com (see "Open questions" below).

## One-line goal
Take a desired portfolio instruction set, authenticate with the user's broker, execute the trades in **one click**, notify the consumer with executed + failed orders.

## Requirement checklist (R-IDs are referenced by beads and the final audit)

| ID | Requirement (from the PDF) | Where satisfied (fill at the end) |
|----|----------------------------|-----------------------------------|
| R1 | Support **at least 5 major Indian brokers** (Zerodha, Fyers, AngelOne, Groww, Upstox/Indiabulls) incl. authentication | `brokers/{zerodha,upstox,fyers,angelone,groww}.py`; auth in each file (README "How each authenticates"); contract suite 5 x 12 cases against respx mocks. NOT live-tested (experimental). |
| R2 | Standardised **Adapter Pattern**; adding a 6th broker needs minimal code change | `brokers/base.py` port + auto-discovery `registry.py`; dummy 6th broker = 1 file, passes contract + listed in `/v1/brokers` (`tests/contract/test_contract.py`); `docs/ADDING_A_BROKER.md`. |
| R3 | If an open-source normaliser is used: written **justification** + able to explain its mechanics | README "Why own adapters" (D1, D25): no normaliser or SDK used; mechanics of each broker auth explained there. |
| R4 | **First-time portfolio** (no holdings): BUY the listed stocks/quantities | `mode=FIRST_TIME` (all BUY, empty holdings required, V1): `planner/`, `tests/unit/test_planner.py`, `tests/engine/test_executor.py`, compose test, `scripts/demo.sh`/`ui_smoke.py`. |
| R5 | **Rebalance**: no delta calculation needed; payload gives explicit SELL / BUY (New) / REBALANCE (Adjust, buy or sell qty change) | Explicit SELL/BUY/REBALANCE payload, sells -> barrier -> buys: `execution/executor.py`; `tests/engine/test_faults.py` (20 seeds), `tests/unit/test_restart_faults.py`. |
| R6 | **Notification** after execution summarising executed trades and failed orders (mock webhook / WS / email / console OK) | `notify/` transactional outbox -> HMAC-signed webhook + console; demo receiver `/mock/webhook`; `tests/engine/test_notifier.py`; compose test and `scripts/demo.sh` receive it. |
| R7 | Backend: **Python + FastAPI** | `src/kalpi_engine/main.py`, `api/` (FastAPI, async). |
| R8 | **Docker**: Dockerfile + docker-compose.yml | `Dockerfile`, `docker-compose.yml` (app + Postgres); `make itest` (3 passed) and `scripts/demo.sh` exit 0 on the compose stack. |
| R9 | Best practices for trading systems: modularity, separation of concerns, **robust handling of API rate limits and failed trades** | Layered modules (README); `execution/limits.py` sliding-window limiter + `Retry-After` + RETRY_SAFE-only retry; write-ahead legs, tag reconciliation, run lease; chaos tests. Gap: adaptive concurrency halving (B8) not built. |
| R10 | Bonus: basic **frontend** (upload target portfolio -> connect broker -> click execute -> view results) | `frontend/index.html` at `/ui` (minimal: file upload .json/.csv + JSON box, parsed in `frontend/portfolio.js`); `tests/unit/test_ui_upload.py`, `scripts/ui_smoke.py` (uploads docs/samples/*), `docs/img/`; manual Chrome click-through on Paper reported by the owner (G3). |
| R11 | Public GitHub repo; **README** with: setup + Docker commands; architecture choices + how rebalance logic works; justification of 3rd-party trading libs | `README.md` (setup + Docker, architecture, rebalance logic, library justification). GitHub repo is currently PRIVATE: must be made public before submission. |
| R12 | Delivered within 24h | Pace tracked in `docs/progress.md` / git history; judged by the submitter at release (G4). |

## Evaluation lens (inferred from Problem 2's criteria; treat as the likely rubric)
1. Systems thinking: correctness under failure, rate limits, partial fills, no double orders.
2. Financial nuance: sells before buys, idempotency, auditability, regulatory awareness (SEBI retail-algo rules).
3. Pragmatism: choose tools for the trade-off they solve, not because they are fashionable.
4. Evidence: it runs (`docker compose up`), tests exist, README is honest about what is live-tested vs mocked.

## Ambiguities in the spec and how we resolve them (document in README)
| # | Ambiguity | Our resolution |
|---|-----------|----------------|
| A1 | Intro says engine "determines the delta", Section 2 says rebalance needs **no** delta calculation | Primary path = explicit instructions (as Section 2). Optional stretch `TARGET_STATE` mode computes the delta (bead S1). |
| A2 | "At least 5 brokers" but reviewers cannot hold 5 live accounts | 5 real adapters + contract tests with mocked HTTP + a first-class **Paper** broker for the demo. README states exactly what was live-tested. |
| A3 | REBALANCE payload shape unspecified | `{symbol, action:"REBALANCE", side:"BUY"|"SELL", quantity>0}` (explicit side beats signed ints; fewer sign bugs). |
| A4 | Order type / product unspecified | Default `MARKET` + `CNC` + `NSE` + `DAY`; per-instruction `LIMIT` override. |
| A5 | What if FIRST_TIME is sent but holdings exist? | Reject whole request (409 `HOLDINGS_EXIST`). Never guess with money. |

## Open questions worth emailing Kalpi (optional, do not block on replies)
1. Should the engine also accept a pure target state and compute the delta, or only explicit instructions?
2. Is a Paper/simulated broker acceptable for the live demo given broker static-IP/daily-2FA constraints?
3. Preferred default for order type (MARKET vs LIMIT) and product (CNC)?

## Problem Statement 2 (later, separate deliverable)
Multi-Frequency Financial Data Platform design doc (1000–1500 words; PIT correctness, tick storage, on-write/read/schedule compute, serving). **Out of scope for this repo/session.** Plan it as its own session after P1 ships.
