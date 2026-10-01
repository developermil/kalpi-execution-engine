# Decisions & Trade-offs (ADR-lite)

Format: **Choice** · why · cost/trade-off · revisit if. Changing a decision = new row at the bottom, never silent edits.

| ID | Decision | Alternatives considered |
|----|----------|-------------------------|
| D1 | **Own adapters over `httpx` (async)**, one file per broker, behind a `BrokerAdapter` port | Official SDKs (kiteconnect, fyers-apiv3, smartapi-python, upstox-python-sdk, growwapi); OpenAlgo as normaliser |
| D2 | FastAPI async + `httpx.AsyncClient` + SQLAlchemy 2 async | Sync stack with thread pools |
| D3 | In-process **asyncio worker with DB write-ahead state + resume on startup** | Celery/RQ + Redis; FastAPI `BackgroundTasks` alone |
| D4 | **Postgres** in compose (SQLAlchemy portable subset; SQLite for fast unit tests) | SQLite only; Mongo |
| D5 | `POST /v1/executions` → **202 + run_id**, poll `GET`, notify via webhook; `Idempotency-Key` required | Long synchronous request |
| D6 | **Explicit instructions** payload; engine validates against live holdings but does not compute delta (stretch S1 adds it) | Always compute delta from target state |
| D7 | **Sells phase → barrier → buys phase** | Fire all orders concurrently |
| D8 | Defaults `MARKET`/`CNC`/`NSE`/`DAY`; `LIMIT` per instruction | Always LIMIT with LTP buffer |
| D9 | **Write-ahead leg state + deterministic order tag + reconcile-before-retry; never auto-resubmit an ambiguous order** | Retry every failure |
| D10 | **Token bucket per (broker, session)** from adapter-declared limits + honour `Retry-After` + jittered backoff + bounded concurrency | Global sleep; naive retry |
| D11 | **Fernet-encrypted** broker tokens at rest; never persist passwords/TOTP seeds; our API guarded by `X-API-Key` | Plain storage; full OAuth for our own API |
| D12 | **Transactional outbox** → webhook (HMAC-SHA256) + console; in-app mock receiver | Fire-and-forget POST |
| D13 | **Canonical instrument = (exchange, symbol)**; each adapter has an `InstrumentResolver` over the broker's instrument master (cached, daily refresh) | Pass raw symbols to brokers |
| D14 | Frontend = one static HTML + vanilla JS served by FastAPI at `/ui`, forms rendered from `GET /v1/brokers` metadata | React/Vite (build step, more tokens/time) |
| D15 | Tests: **shared adapter contract suite** (respx mocks) + Paper broker with fault injection; live tests opt-in (`-m live`) | Only unit tests; only live tests |
| D16 | **Paper broker is first-class** and selectable in UI/API | Hide it as a test double |
| D17 | Regulatory awareness baked into adapter metadata (`requires_static_ip`, `daily_2fa`) and README | Ignore regulation |

## D1 — Broker integration approach (the one the reviewers will probe)
- **Choice:** own thin adapters on `httpx`.
- **Why:** (1) async-native, so one event loop drives many broker calls; most official SDKs are synchronous wrappers and would need thread pools and hide retry behaviour. (2) The crux of this problem is *failure semantics* (timeout after send, 429, partial reject). Owning the HTTP layer lets us classify each error as retry-safe vs ambiguous. (3) Smaller dependency tree and a smaller Docker image. (4) The brief demands we can explain the mechanics: ~100–150 lines per adapter is explainable; a third-party abstraction is not.
- **Why not OpenAlgo:** AGPL-3.0 (network copyleft) is a poor fit for a commercial platform; it is a standalone server with its own auth/DB/symbol-mapping store, adding a hop and a second source of truth; its normalisation can hide the order states we must surface. Studying its approach is fine; copying code is not.
- **Cost:** more code written by us; endpoint details must be verified against official docs (bead A1); we cannot live-test all five brokers (static IP, daily 2FA, funded accounts).
- **Mitigation:** contract tests built from documented request/response shapes; clear `UNVERIFIED` flags; live smoke test on one broker the builder actually has; Paper broker for the demo.
- **Fallback cascade (failure F1):** official SDK wrapped in `asyncio.to_thread` for that broker → ship adapter marked `experimental` with contract tests → drop to Paper only and say so.
- **Revisit if:** >10 brokers needed, or Kalpi already runs a broker-gateway.

## D3 — Where does execution run?
- **Choice:** asyncio tasks in the API process; every state change is committed to Postgres *before* the broker call (write-ahead). On startup, non-terminal runs are resumed: legs in `SUBMITTING` are reconciled against the broker order book (by tag) before anything is re-sent.
- **Trade-off:** single process, no horizontal scale, no priority queues. Acceptable for a 24h build and honest in README; a queue (Celery/Arq) becomes the next step. The state model already supports it because workers only read/write DB rows.

## D7 — Sells before buys
- Sells free cash (and margin) so buys do not fail on funds. Phase barrier: wait for sells to reach a terminal state (or timeout) before buys.
- **Do not assume** same-day usability of sale proceeds: treat as broker-specific; offer optional funds pre-check via the adapter's `get_funds`.
- `halt_on_sell_failure` (default `false`): when `true`, skip buys if any sell failed. Default continues because rebalance legs are usually independent; the result lists everything.

## D9 — Never double-trade (money safety)
1. Leg state `SUBMITTING` committed before the HTTP call.
2. Each leg gets a deterministic **tag** (<=20 chars, derived from run_id+leg index) sent as the broker's tag/reference field where supported.
3. Error classes: `RETRY_SAFE` (429, connect error before send), `REJECTED` (4xx validation), `AMBIGUOUS` (read timeout, 5xx after send).
4. `AMBIGUOUS` → poll `find_order_by_tag` (3 tries, backoff). Found → adopt. Not found → leg `UNKNOWN`, **no auto-resubmit**, surfaced in notification.
- **Cost:** some legs need a human; that is the correct failure mode for real money.

## D10 — Rate limits
- Adapter declares `RateLimits(per_second, per_minute)`; defaults to 10/s (the SEBI retail cap reported by brokers) and 200/min unless docs say otherwise (to verify in A1).
- Token bucket + `asyncio.Semaphore` for concurrency; on 429 honour `Retry-After`, else exponential backoff with jitter; after N hits shrink concurrency by half for that session.

## D13 — Instrument mapping (the hidden hard part)
Likely identifier per broker (**verify in A1**): Zerodha `tradingsymbol`+exchange; Fyers `NSE:SYMBOL-EQ`; AngelOne `symboltoken` + `SYMBOL-EQ` (from scrip master); Upstox `instrument_key` like `NSE_EQ|<ISIN>` (from instruments file); Groww `trading_symbol`+exchange+segment. The resolver is an adapter-owned component with a cached master file and a `UNKNOWN_SYMBOL` failure before any order is sent.

## D17 — Regulation (reported by brokers, verify yourself)
Fyers' notice describes the SEBI retail algo framework effective 1 Apr 2026: orders only from a registered app ID mapped to a whitelisted **static IP**, **2FA once per trading day** (no persistent refresh sessions), **10 orders/sec** cap, **market orders converted to MPP**, third-party platforms must be empanelled by the broker. Groww docs do not mention a static IP requirement. Consequence: live tests need a static IP and fresh daily login; a reviewer cannot run live. Document in README under "Regulatory notes".
