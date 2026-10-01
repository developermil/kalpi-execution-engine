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
| D18 | **G0 counts BLOCKING UNVERIFIED items only** (token exchange, holdings qty, place-order + delivery code, status vocab, tag, rate limits, instrument master). Sandbox, pricing, MPP %, secondary error shapes, regulatory nuance and funds (`get_funds` may return None) are NON-BLOCKING | Count every UNVERIFIED cell (rewards vague notes, punishes honest ones) |
| D19 | **Defensive mapping for unverified facts**: unknown status is never FILLED and never resubmitted (it becomes UNKNOWN); sellable qty = the most conservative field; conflicting rate limits mean we use the lowest; unknown tag limit means the shorter value | Optimistic guesses; block the adapter entirely |
| D20 | **Run lease** (`runs.lease_owner`, `lease_until`, claimed by conditional `UPDATE ... WHERE lease_until < now`, renewed every TTL/3) + leg compare-and-set `PLANNED->SUBMITTING`; uvicorn pinned to `--workers 1` (plan-critic #1) | Postgres advisory lock (not portable to SQLite); trust single process |
| D21 | **Unresolved sells are failures at the barrier**: only `FILLED` counts as OK; `UNKNOWN`/`OPEN`/`PARTIAL` (and rejects) count for `halt_on_sell_failure`. `UNKNOWN` legs are re-checked by tag at T+60s and at finalise; a late hit updates the leg and emits `execution.updated` (plan-critic #2). Amends D7 | Treat "terminal or timed out" as done; never re-check |
| D22 | **G0 closes on PASS, or ADJUST with F1 actions recorded per broker; never ABORT**. BLOCKING threshold: <=2 PASS, 3–6 ADJUST (so fyers and upstox, 3 each, are ADJUST). Verdict is recorded only by `/gate` after A5 (plan-critic #3) | G0 requires PASS (deadlocks the plan) |
| D23 | **Explicit transport classification table** (SPEC §4): connect-phase errors + 429 RETRY_SAFE; read/write timeouts, read/protocol errors, 5xx AMBIGUOUS; 401/403 TokenException/AG8001 AUTH_EXPIRED; one test per class (plan-critic #4). Amends D9.3 | "Connect error before send" prose only |
| D24 | **MARKET orders are allowed on every broker**; each contract fixture asserts the outgoing payload (Zerodha `market_protection=-1`, AngelOne `price=0`, Upstox `slice=false`). `BrokerMeta.market_order_verified=false` where MPP handling is UNVERIFIED (Groww): preview warns (V11), no hard reject (plan-critic #5, amended by human) | Hard-reject MARKET on Groww; default all orders to LIMIT |
| D25 | **Never wrap an official SDK's `place_order`** (or any order call); at most copy SDK constants/enums/paths into our httpx adapter. Supersedes the D1 fallback "SDK in `asyncio.to_thread`" and F1 step 2 (plan-critic #6, amended by human) | Wrap SDK in `to_thread` (unmockable by respx; hides timeout-before/after-send) |
| D26 | **`Holding.sellable_qty`** computed per adapter from the most conservative field(s); V4 validates against it (plan-critic #7) | Validate against total `quantity` |
| D27 | **Validate before insert; idempotency keyed on `(user_id, key)` + `request_hash`**: 4xx creates no run; same key + different body = 422 `IDEMPOTENCY_KEY_REUSED` (plan-critic #8) | Insert first, persist failed runs |
| D28 | **Explicit leg->run status table** (SPEC §3); summary has a count per leg status incl. `partial`; PARTIAL appears in `executed` (filled qty) and `failed` (remainder) (plan-critic #9) | Implicit "counts add up" |
| D29 | **`BrokerMeta.experimental` + `live_tested`** exposed via `GET /v1/brokers`; ambiguous orders are adopted **only by tag**; order-book heuristics are report-only (`possible_matches`, never adopted) (plan-critic #12) | Heuristic adoption (`matched_heuristically`) |
| D30 | **Lease fencing + periodic re-claim**: every execution-time UPDATE carries the lease predicate; renew runs in its own task; a resumed `SUBMITTING` leg whose tag lookup misses becomes `UNKNOWN`, never resent; the worker re-sweeps expired leases every `LEASE_TTL_S` (plan-critic #13, #14). Amends D20 | Startup-only scan; resend on tag miss |
| D31 | **Durable recheck, kept minimal**: `legs.recheck_at` swept by the worker for `UNKNOWN`/`OPEN`/`PARTIAL` legs, CAS on status, until 15:35 IST; per-run integer `events.seq`, latest carried in the payload (plan-critic #15, #21; human: nothing more). Replaces D21's in-memory T+60s timer | In-memory timers; full scheduler; event versioning |
| D32 | **Auth expiry mapping**: `PLANNED` -> `SKIPPED`, `SUBMITTED`/`OPEN` -> `UNKNOWN(AUTH_EXPIRED)`, `PARTIAL` kept; run status only from the SPEC §3 table (plan-critic #18) | Force `COMPLETED_WITH_FAILURES` |
| D33 | **Every gate closes on PASS, or ADJUST with that row's actions done and recorded**; ABORT stops and escalates. Extends D22 to G1–G4; G4 audits R1–R12 (plan-critic #19) | PASS-only gates (deadlock) |
| D34 | **HTTP 200 with an error body**: known code / `status:false` -> `REJECTED` or `AUTH_EXPIRED`; `place_order` 200 with unparseable body or no order id -> `AMBIGUOUS`; one contract case per adapter (plan-critic #20). Extends D23 | Trust HTTP status only |
| D35 | **Scope cuts for slack**: instrument master loaded at startup only (refresh = restart; amends D13 "daily refresh"); adaptive concurrency halving moved to P1 bead B8 (amends D10) (plan-critic #22) | Keep both in P0 |
| D36 | **Non-terminal legs at finalisation count as in doubt**: `run_status()` treats `PLANNED`/`SUBMITTING`/`SUBMITTED` like `UNKNOWN`, so a run with any such leg is `COMPLETED_WITH_FAILURES`, never `FAILED` (an order may exist). Executor should map them first (D30/D32); this is the backstop. Pinned by `test_run_status_non_terminal_never_failed`. Extends D28 | Treat as `FAILED`; raise on non-terminal input |
| D37 | **G0 ADJUST on elapsed time (H4:35 at gate vs <=H4:30)**: per the G0 row, D1 is cut to the F12 minimal page (broker select + JSON textarea + Connect/Preview/Execute + leg polling; no CSV upload or generated forms), est 90->45m; ui_smoke still must reach COMPLETED for first-time and rebalance. README discloses the UI as minimal | Keep full D1 and eat buffer |
| D38 | **Sliding-window limiter instead of token bucket**: `RateLimiter` admits at most `n` calls in any window of `window_s` (log of timestamps). A token bucket of capacity r allows up to 2r calls across a 1s boundary (full bucket + refill), breaching a broker's hard per-second cap (e.g. SEBI 10/s). Rate >= 1/s: `n = floor(rate)`, window 1s (2.5/s -> 2/s, conservative). Rate < 1/s: never rounds to 0; clamped to `n = 1` per `1/rate` s (0.5/s -> 1 call per 2s). Pinned by `tests/engine/test_limits.py` (B3). Amends D10 | Classic token bucket; round fractional rates up |
| D39 | **Test fake clock advances only when no DB call is in flight** (`tests/engine/fakes.py`): `FakeClock` is a virtual-time scheduler; its ticker moves time to the earliest sleeper only when no aiosqlite call is running (counted at `Connection._execute`/`_connect`) and the loop is quiescent; tests wait on events, never by polling the DB (which would freeze the clock). **Why:** the B4c lease renewer made the old clock flaky: time advanced ~77x faster than real time (1 fake s per ~13 ms Windows tick), so one slow SQLite commit burned >30 fake s, expiring the lease or blowing poll deadlines mid-run. Took 4 attempts (F11 overrun, see progress.md) | Real time in engine tests (slow); clock jumps per sleeper (flaky with concurrent renewer) |
| D40 | **`GET /v1/brokers` is unauthenticated** (human, B6): it returns adapter metadata only (auth mode, credential field names, rate limits, flags), never secrets or user data, and the UI needs it to render connect forms before login. Every other `/v1/*` route requires `X-API-Key`; `/v1/sessions/callback` is authenticated by the one-time OAuth `state` instead | Require `X-API-Key` on every route |
| D41 | **OAuth login `state` kept in process memory** (human, B6): `state -> (user_id, broker, issued_at)`, single-use, 10 min TTL. Consequences, listed in README Known limitations (`docs/LIMITATIONS.md`): a restart during an OAuth login means restarting the login; only valid with one worker (already pinned by D20) | Persist states in a DB table |
| D42 | **Pace is judged against the 13:45 submission deadline, not elapsed hours** (human, after B6): the G0 "Elapsed since start" row (H4:30 / H6:00) is stale and no longer applies; S* beads are kept (they stay blocked behind G4) | Drop S* at H6:00 |
| D43 | **Bead order after B6: B7 -> /gate G1 -> E2 -> C0** (human), encoded as dependencies: E2 blocked_by G1, C0 blocked_by E2 | C0 first (plan order) |
| D44 | **Event seq allocation locks the run row** (found by E2 on Postgres): `append_event` does `SELECT runs.id ... FOR UPDATE` before `max(seq)+1`. Concurrent legs of one run raced under READ COMMITTED -> UNIQUE(run_id,seq) violation killed the executor task; the lease sweeper resumed it ~60s later (no double orders, but a stall). SQLite serialises writers, so B7/G1 could not see it. E2 now asserts no `run.resumed` in healthy runs | Retry on IntegrityError; Postgres sequence per run; one shared global seq |

## D1 — Broker integration approach (the one the reviewers will probe)
- **Choice:** own thin adapters on `httpx`.
- **Why:** (1) async-native, so one event loop drives many broker calls; most official SDKs are synchronous wrappers and would need thread pools and hide retry behaviour. (2) The crux of this problem is *failure semantics* (timeout after send, 429, partial reject). Owning the HTTP layer lets us classify each error as retry-safe vs ambiguous. (3) Smaller dependency tree and a smaller Docker image. (4) The brief demands we can explain the mechanics: ~100–150 lines per adapter is explainable; a third-party abstraction is not.
- **Why not OpenAlgo:** AGPL-3.0 (network copyleft) is a poor fit for a commercial platform; it is a standalone server with its own auth/DB/symbol-mapping store, adding a hop and a second source of truth; its normalisation can hide the order states we must surface. Studying its approach is fine; copying code is not.
- **Cost:** more code written by us; endpoint details must be verified against official docs (bead A1); we cannot live-test all five brokers (static IP, daily 2FA, funded accounts).
- **Mitigation:** contract tests built from documented request/response shapes; clear `UNVERIFIED` flags; live smoke test on one broker the builder actually has; Paper broker for the demo.
- **Fallback cascade (failure F1, amended by D25):** copy constants/enums/paths from the official SDK source into our httpx adapter (**never wrap the SDK's `place_order` or any order call**) → ship adapter marked `experimental` with contract tests → drop to Paper only and say so.
- **Revisit if:** >10 brokers needed, or Kalpi already runs a broker-gateway.

## D3 — Where does execution run?
- **Choice:** asyncio tasks in the API process; every state change is committed to Postgres *before* the broker call (write-ahead). On startup, non-terminal runs are resumed: legs in `SUBMITTING` are reconciled against the broker order book (by tag) before anything is re-sent. A run is executed only by the holder of its lease (D20); the container runs one uvicorn worker.
- **Trade-off:** single process, no horizontal scale, no priority queues. Acceptable for a 24h build and honest in README; a queue (Celery/Arq) becomes the next step. The state model already supports it because workers only read/write DB rows.

## D7 — Sells before buys
- Sells free cash (and margin) so buys do not fail on funds. Phase barrier: wait for sells to reach a terminal state (or timeout) before buys.
- **Do not assume** same-day usability of sale proceeds: treat as broker-specific; offer optional funds pre-check via the adapter's `get_funds`.
- `halt_on_sell_failure` (default `false`): when `true`, skip buys if any sell failed. A sell left `UNKNOWN`/`OPEN`/`PARTIAL` counts as failed (D21). Default continues because rebalance legs are usually independent; the result lists everything.

## D9 — Never double-trade (money safety)
1. Leg state `SUBMITTING` committed before the HTTP call.
2. Each leg gets a deterministic **tag** (<=20 chars, derived from run_id+leg index) sent as the broker's tag/reference field where supported.
3. Error classes: `RETRY_SAFE` (429, connect error before send), `REJECTED` (4xx validation), `AMBIGUOUS` (read timeout, 5xx after send). Exact exception table: SPEC §4 (D23).
4. `AMBIGUOUS` → poll `find_order_by_tag` (3 tries, backoff). Found → adopt. Not found → leg `UNKNOWN`, **no auto-resubmit**, surfaced in notification; re-checked by tag at T+60s and at finalise (D21). Never adopted heuristically (D29).
- **Cost:** some legs need a human; that is the correct failure mode for real money.

## D10 — Rate limits
- Adapter declares `RateLimits(per_second, per_minute)`; defaults to 10/s (the SEBI retail cap reported by brokers) and 200/min unless docs say otherwise (to verify in A1).
- Sliding-window limiter (D38; was token bucket) + `asyncio.Semaphore` for concurrency; on 429 honour `Retry-After`, else exponential backoff with jitter; after N hits shrink concurrency by half for that session (P1, bead B8; D35).

## D13 — Instrument mapping (the hidden hard part)
Likely identifier per broker (**verify in A1**): Zerodha `tradingsymbol`+exchange; Fyers `NSE:SYMBOL-EQ`; AngelOne `symboltoken` + `SYMBOL-EQ` (from scrip master); Upstox `instrument_key` like `NSE_EQ|<ISIN>` (from instruments file); Groww `trading_symbol`+exchange+segment. The resolver is an adapter-owned component with a cached master file and a `UNKNOWN_SYMBOL` failure before any order is sent.

## D17 — Regulation (reported by brokers, verify yourself)
Fyers' notice describes the SEBI retail algo framework effective 1 Apr 2026: orders only from a registered app ID mapped to a whitelisted **static IP**, **2FA once per trading day** (no persistent refresh sessions), **10 orders/sec** cap, **market orders converted to MPP**, third-party platforms must be empanelled by the broker. Groww docs do not mention a static IP requirement. Consequence: live tests need a static IP and fresh daily login; a reviewer cannot run live. Document in README under "Regulatory notes".
