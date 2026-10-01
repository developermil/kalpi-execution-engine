# SPEC — Portfolio Trade Execution Engine (v1, after plan-critic review 2026-10-01)

Read only the section you need (`grep -n '^## ' docs/SPEC.md`).

## 1. Repository layout
```
src/kalpi_engine/
  api/            routers (brokers, sessions, executions, mock_webhook), deps, errors
  domain/         pydantic models, enums, errors  (no I/O, no framework imports)
  planner/        payload + holdings -> ordered OrderIntents (pure functions)
  brokers/        base.py (port + HttpBrokerAdapter), registry.py, paper.py,
                  zerodha.py upstox.py fyers.py angelone.py groww.py, instruments.py
  execution/      executor.py, ratelimit.py, retry.py, reconcile.py, worker.py
  notify/         outbox.py, webhook.py, console.py
  storage/        models.py, repo.py, crypto.py, db.py
  config.py  main.py  logging.py
frontend/index.html   (served at /ui)
tests/ unit/ contract/ engine/ integration/
```
Dependency rule: `domain` imports nothing internal; `planner` imports `domain`; `execution` imports `domain`, `brokers.base`, `storage`; `api` imports everything via service layer. No broker module imports another broker module.

## 2. HTTP API
Auth to our API: `X-API-Key` (maps to `user_id`). All bodies JSON. Errors: `{"error":{"code","message","details"}}`.

| Method & path | Purpose |
|---|---|
| `GET /v1/brokers` | List adapters: `id, name, auth_mode, credential_fields[], rate_limits, requires_static_ip, daily_2fa, experimental, live_tested, market_order_verified`. UI renders forms from this. |
| `GET /v1/sessions/login-url?broker=` | OAuth brokers: returns `{login_url, state}` |
| `GET /v1/sessions/callback?broker=&state=&...` | Completes OAuth; stores encrypted session; redirects to `/ui?session_id=` |
| `POST /v1/sessions` `{broker, credentials{}}` | Credential/API-key brokers (AngelOne client+PIN+TOTP, Groww key+secret/TOTP, Paper none) -> `{session_id, expires_at}` |
| `POST /v1/executions/preview` | Same body as execute; validates against live holdings; returns plan + warnings; **no orders** |
| `POST /v1/executions` | Header `Idempotency-Key` (required). Validates **synchronously before any insert** (a 4xx creates no run). Returns `202 {run_id,status}`; same key + same `request_hash` => same run (200); same key + different body => 422 `IDEMPOTENCY_KEY_REUSED` (D27) |
| `GET /v1/executions/{run_id}` | Run, legs, events, notification status |
| `GET /v1/executions?limit=` | Recent runs |
| `POST /mock/webhook` | Demo consumer: verifies HMAC, logs, stores last N |
| `GET /healthz`, `/readyz` | liveness; DB + broker registry ready |

### Execute / preview body
```json
{
  "session_id": "uuid",
  "mode": "FIRST_TIME | REBALANCE",
  "options": {
    "order_type": "MARKET", "product": "CNC", "exchange": "NSE",
    "halt_on_sell_failure": false, "poll_timeout_s": 60, "webhook_url": null
  },
  "instructions": [
    {"symbol":"RELIANCE","action":"BUY","quantity":10},
    {"symbol":"TCS","action":"SELL","quantity":5},
    {"symbol":"INFY","action":"REBALANCE","side":"BUY","quantity":3},
    {"symbol":"HDFCBANK","action":"BUY","quantity":4,"order_type":"LIMIT","limit_price":1650.0}
  ]
}
```
FIRST_TIME: every action is `BUY`. REBALANCE: `SELL` (exit qty), `BUY` (new stock), `REBALANCE` (adjust existing, `side` required).

### Validation (whole request rejected with 422/409 and **all** violations listed; nothing is sent to the broker)
V1 FIRST_TIME needs empty holdings, else 409 `HOLDINGS_EXIST`; all actions BUY.
V2 symbols unique within a request. V3 quantity positive integer, `<= MAX_QTY_PER_ORDER`.
V4 SELL / REBALANCE-SELL quantity `<=` `Holding.sellable_qty` (`INSUFFICIENT_HOLDINGS`; D26).
V5 `BUY` of an already-held symbol => **warning** (not error). V6 REBALANCE requires `side`.
V7 LIMIT requires `limit_price>0`. V8 every symbol resolvable by the adapter (`UNKNOWN_SYMBOL`).
V9 session exists and not expired (`SESSION_EXPIRED`, 401). V10 outside 09:15–15:30 IST Mon–Fri => **warning** (broker is authoritative; holidays unknown).
V11 a MARKET leg on a broker with `meta.market_order_verified=false` => **warning** `MARKET_ORDER_UNVERIFIED` (suggest LIMIT); never a hard reject (D24).

## 3. Domain model (names are contracts)
- `Side{BUY,SELL}`, `OrderType{MARKET,LIMIT}`, `Product{CNC}`, `Exchange{NSE,BSE}`
- `Instruction{symbol, action, side?, quantity, order_type?, limit_price?}`
- `OrderIntent{leg_id, phase(SELL|BUY), symbol, exchange, side, quantity, order_type, limit_price, tag}`
- `Holding{symbol, exchange, quantity, sellable_qty, avg_price?}`; `sellable_qty` is computed by the adapter from the most conservative field(s), e.g. Zerodha `quantity - used_quantity - collateral_quantity` (D19/D26); `Funds{available_cash}`
- `BrokerOrderState{broker_order_id, status(OPEN|PARTIAL|FILLED|REJECTED|CANCELLED), filled_qty, avg_price?, message?}`
- Run status: `CREATED -> RUNNING -> COMPLETED | COMPLETED_WITH_FAILURES | FAILED` (validation failures never create a run)
- Leg status: `PLANNED -> SUBMITTING -> SUBMITTED -> OPEN|PARTIAL -> FILLED | REJECTED | CANCELLED | FAILED | UNKNOWN | SKIPPED`
- Leg -> run status (I7, pure function of final leg statuses; D28):

| Condition over all legs | Run status |
|---|---|
| every leg `FILLED` | `COMPLETED` |
| no leg `FILLED`/`PARTIAL` and no leg `OPEN`/`UNKNOWN` (nothing executed, nothing in doubt) | `FAILED` |
| anything else (any `PARTIAL`, `OPEN`, `UNKNOWN`, or a mix of filled and not filled) | `COMPLETED_WITH_FAILURES` |

- Sell resolution at the barrier (D21): a sell is **resolved-OK** only if `FILLED`. `PARTIAL`, `OPEN`, `UNKNOWN`, `REJECTED`, `CANCELLED`, `FAILED`, `SKIPPED` are **sell failures** for `halt_on_sell_failure`.
- Tag: `make_tag(run_id, leg_index, length)` = `"K" + base32(sha1(run_id + ":" + leg_index))[:length-1]`, alphanumeric; `length = min(16, meta.tag_max_len)`, must be 8..20 (Groww min 8). `BrokerMeta.tag_max_len: int` (default 20; per-broker values in BROKERS.md; unknown = shorter value, D19)

## 4. Adapter port (the whole contract a 6th broker implements)
```python
class BrokerAdapter(ABC):
    meta: BrokerMeta                      # id, name, auth_mode, credential_fields, rate_limits, requires_static_ip, daily_2fa, tag_max_len,
                                          # experimental: bool, live_tested: bool, market_order_verified: bool (D24/D29)
    async def login_url(self, state: str) -> str: ...              # OAUTH_REDIRECT only
    async def create_session(self, params: dict) -> BrokerSession: ...   # callback params or credentials
    async def get_holdings(self, s: BrokerSession) -> list[Holding]: ...
    async def get_funds(self, s: BrokerSession) -> Funds | None: ...
    async def resolve_instrument(self, exchange, symbol) -> InstrumentRef: ...
    async def place_order(self, s, intent: OrderIntent) -> str: ...      # returns ONE broker_order_id; send params that
                                                                         # prevent slicing (Upstox `slice=false`, take order_ids[0])
    async def get_order(self, s, broker_order_id: str) -> BrokerOrderState: ...
    async def find_order_by_tag(self, s, tag: str) -> BrokerOrderState | None: ...
    async def cancel_order(self, s, broker_order_id: str) -> None: ...   # optional
```
`HttpBrokerAdapter` (base) gives: shared `httpx.AsyncClient` with timeouts, the rate limiter, and `classify(response|exception) -> RETRY_SAFE | REJECTED | AMBIGUOUS | AUTH_EXPIRED`; adapters override only request building and response parsing. Registration is **auto-discovery** (`pkgutil` over `brokers/`) so a 6th broker = one new file, zero edits elsewhere. `auth_mode ∈ {OAUTH_REDIRECT, CREDENTIALS_TOTP, API_KEY_SECRET, NONE}`.

Transport/response classification (`classify`, D23); one test per row:

| Signal | Class |
|---|---|
| `httpx.ConnectError`, `ConnectTimeout`, `PoolTimeout` (request never left) | `RETRY_SAFE` |
| HTTP 429 | `RETRY_SAFE` (honour `Retry-After`) |
| `httpx.ReadTimeout`, `WriteTimeout`, `ReadError`, `RemoteProtocolError`, any 5xx | `AMBIGUOUS` |
| 401, Zerodha 403 `TokenException`, AngelOne `AG8001` (plus per-adapter auth codes) | `AUTH_EXPIRED` |
| other 4xx / broker validation error | `REJECTED` |

Order payloads are asserted by each adapter's contract fixture (D24): Zerodha MARKET sends `market_protection=-1`; AngelOne MARKET sends `price=0`; Upstox sends `slice=false`; Groww sends its documented payload and declares `market_order_verified=false`. Adapters never wrap an official SDK's order call (D25).

Error taxonomy (`domain/errors.py`): `AuthExpired`, `RateLimited(retry_after)`, `InvalidOrder(code)`, `InsufficientFunds`, `BrokerRejected(message)`, `TransientError` (retry-safe), `AmbiguousSubmit` (must reconcile), `UnknownSymbol`.

## 5. Execution algorithm
1. Look up `(user_id, idempotency_key)`: exists with same `request_hash` (sha256 of canonical body) => return it; different hash => 422 `IDEMPOTENCY_KEY_REUSED`.
2. Load holdings; run validation V1–V11 **synchronously**; any violation => 4xx, nothing persisted. Then, in one transaction, insert the run (unique `(user_id, idempotency_key)`; a racing duplicate insert falls back to step 1) and the planner legs: all SELL legs (phase 1) then all BUY legs (phase 2), `PLANNED`, + audit event.
3. Claim the run lease (D20): `UPDATE runs SET lease_owner=:me, lease_until=:now+LEASE_TTL_S WHERE id=:id AND (lease_until IS NULL OR lease_until < :now)`; rowcount 0 => another worker owns it, do nothing. Renew every `LEASE_TTL_S/3` with `WHERE lease_owner=:me`; a failed renew stops placing immediately. Portable SQL (SQLite + Postgres).
4. For each phase, bounded concurrency; per leg: compare-and-set `PLANNED -> SUBMITTING` (`WHERE status='PLANNED'`, rowcount must be 1) -> limiter -> `place_order` with retry policy (RETRY_SAFE only) -> `SUBMITTED` -> poll `get_order` until terminal or `poll_timeout_s` (then leave `OPEN`, never auto-cancel).
5. AMBIGUOUS -> reconcile via `find_order_by_tag` (3 tries) -> adopt, else `UNKNOWN` (no resubmit). Never adopt by symbol/side/qty heuristics (D29).
6. `AuthExpired` mid-run -> stop placing; remaining legs `SKIPPED(reason=AUTH_EXPIRED)`; run `COMPLETED_WITH_FAILURES`.
7. Barrier between phases: wait until every sell is terminal, `UNKNOWN`, or past `poll_timeout_s`; count sell failures per §3 (D21); apply `halt_on_sell_failure`.
8. Late reconcile (D21): every `UNKNOWN` leg is re-checked by tag at T+60s after it became `UNKNOWN` and again at finalise; a hit updates the leg (+ event). Finalise: re-check, compute status (§3), write outbox row in the **same transaction**; worker delivers webhook/console. A T+60s hit after finalise updates the leg, recomputes status and writes a new outbox row with `event=execution.updated`.
9. Startup: resume non-terminal runs only through the lease (step 3); reconcile `SUBMITTING` legs by tag first. Deployment runs exactly one uvicorn worker (`--workers 1`); the lease is the safety net, not the scaling model.

## 6. Notification payload (webhook JSON; header `X-Kalpi-Signature: sha256=<hmac>`)
```json
{ "event":"execution.completed", "run_id":"...", "mode":"REBALANCE", "broker":"upstox",
  "status":"COMPLETED_WITH_FAILURES",
  "summary":{"total":8,"filled":5,"partial":1,"open":0,"rejected":1,"cancelled":0,"failed":0,"unknown":1,"skipped":0},
  "executed":[{"symbol":"TCS","side":"SELL","qty":5,"filled_qty":5,"avg_price":3890.5,"broker_order_id":"..."}],
  "failed":[{"symbol":"INFY","side":"BUY","qty":3,"status":"REJECTED","reason":"RMS: insufficient funds"}],
  "started_at":"...", "finished_at":"..." }
```
Counts: `total` = sum of the per-status counts (I4). A `PARTIAL` leg appears in `executed` (with `filled_qty`) **and** in `failed` (`qty` = remainder, `status:"PARTIAL"`). `event` is `execution.completed`, or `execution.updated` after a late reconcile (§5 step 8).
Delivery: at-least-once, exponential backoff (1,2,4,8,16s), then dead-letter flag; result always retrievable via `GET /v1/executions/{id}`.

## 7. Tables
`broker_sessions(id,user_id,broker,token_enc,expires_at,meta_json)` · `runs(id,user_id,idempotency_key UNIQUE per user,request_hash,session_id,mode,status,options_json,lease_owner,lease_until,created_at,finished_at)` · `legs(id,run_id,idx,phase,symbol,exchange,side,qty,order_type,limit_price,tag,status,broker_order_id,filled_qty,avg_price,reason)` · `events(id,run_id,leg_id?,ts,type,payload_json)` (append-only audit) · `outbox(id,run_id,url,payload_json,attempts,next_attempt_at,status)`.

## 8. Config (env)
`DATABASE_URL, FERNET_KEY, API_KEYS (user:key pairs), WEBHOOK_SECRET, DEFAULT_WEBHOOK_URL, MAX_QTY_PER_ORDER, MAX_CONCURRENCY, LEASE_TTL_S (default 30), LOG_LEVEL`. Dockerfile pins `uvicorn ... --workers 1` (D20). Never commit `.env`.
