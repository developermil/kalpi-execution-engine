# Assignment: Problem Statement 1 — Portfolio Trade Execution Engine

Source: Kalpi Builder Assignment v2 (Backend/Infra Engineer). Time allowance: 24h from receipt.
Questions to Kalpi: info@kalpicapital.com (see "Open questions" below).

## One-line goal
Take a desired portfolio instruction set, authenticate with the user's broker, execute the trades in **one click**, notify the consumer with executed + failed orders.

## Requirement checklist (R-IDs are referenced by beads and the final audit)

| ID | Requirement (from the PDF) | Where satisfied (fill at the end) |
|----|----------------------------|-----------------------------------|
| R1 | Support **at least 5 major Indian brokers** (Zerodha, Fyers, AngelOne, Groww, Upstox/Indiabulls) incl. authentication | |
| R2 | Standardised **Adapter Pattern**; adding a 6th broker needs minimal code change | |
| R3 | If an open-source normaliser is used: written **justification** + able to explain its mechanics | |
| R4 | **First-time portfolio** (no holdings): BUY the listed stocks/quantities | |
| R5 | **Rebalance**: no delta calculation needed; payload gives explicit SELL / BUY (New) / REBALANCE (Adjust, buy or sell qty change) | |
| R6 | **Notification** after execution summarising executed trades and failed orders (mock webhook / WS / email / console OK) | |
| R7 | Backend: **Python + FastAPI** | |
| R8 | **Docker**: Dockerfile + docker-compose.yml | |
| R9 | Best practices for trading systems: modularity, separation of concerns, **robust handling of API rate limits and failed trades** | |
| R10 | Bonus: basic **frontend** (upload target portfolio -> connect broker -> click execute -> view results) | |
| R11 | Public GitHub repo; **README** with: setup + Docker commands; architecture choices + how rebalance logic works; justification of 3rd-party trading libs | |
| R12 | Delivered within 24h | |

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
