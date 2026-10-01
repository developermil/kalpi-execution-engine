# Kalpi Execution Engine: project context (knowledge file)

State as of commit `fbc5111` (2026-10-02). Repo: https://github.com/developermil/kalpi-execution-engine (public).
Read this first, then open only the file a task needs. Facts below were read from the repo; items
marked UNVERIFIED were never confirmed against a real broker.

## 1. What this is
- **Kalpi Builder Assignment v2, Problem Statement 1**: a portfolio trade execution engine for a
  systematic quant investing platform in India. Take an explicit instruction set, authenticate with
  the user's broker, execute the orders in one call, notify a consumer of executed and failed orders.
- Requirements R1-R12 live in `docs/ASSIGNMENT.md` (each row has a "Where satisfied" entry).
  R1 five brokers, R2 adapter pattern, R3 justify libs, R4 first-time portfolio, R5 rebalance
  (explicit SELL/BUY/REBALANCE, no delta), R6 notification, R7 FastAPI, R8 Docker, R9 rate limits and
  failed trades, R10 bonus UI, R11 public repo + README, R12 24 h.
- Owner: Milind. Stack: Python 3.12, FastAPI, async SQLAlchemy 2 (Postgres in compose, SQLite in unit
  tests), httpx, pydantic v2, uv, ruff, mypy (strict, domain only), pytest. Only dependencies:
  fastapi, uvicorn, pydantic(-settings), httpx, sqlalchemy, asyncpg, cryptography.
- Status: phases A, C, D, E done; B8 (adaptive concurrency halving, P1) and S1-S3 (stretch) open.
  Gates G0-G4 passed. **All five real adapters are tested against mocked HTTP only**
  (`experimental=true`, `live_tested=false`). The engine is tested end to end on the Paper broker.

## 2. Repo map
- `src/kalpi_engine/`: `domain/` (models, enums, errors, status, tags; imports nothing internal, no
  I/O), `planner/` (pure: validate V1-V11, plan legs), `brokers/` (`base.py` port + shared httpx
  client + classification, `registry.py` auto-discovery, `instruments.py`, `paper.py`, `zerodha.py`,
  `upstox.py`, `fyers.py`, `angelone.py`, `groww.py`), `execution/` (executor, lease, legs, limits,
  reconcile, resume, finalise), `notify/` (outbox worker, HMAC signing, payload), `storage/` (db,
  repo, Fernet crypto), `api/` (brokers, sessions, executions, mock_webhook, ui), `logging.py`,
  `config.py`, `main.py`, `service.py`.
- `frontend/index.html` + `frontend/portfolio.js`: the UI at `/ui` and its .json/.csv parser.
- `tests/`: `unit/`, `engine/` (executor, faults, lease, limits, notifier, reconcile),
  `contract/` (shared adapter suite, per-broker fixtures in `fixtures/<id>.py`, dummy 6th broker in
  `_dummy/`), `integration/test_compose.py` (needs the compose stack).
- `docs/`: `SPEC.md` (contracts), `DECISIONS.md` (D1-D45), `PLAN.md` (gates, failure cascades),
  `ASSIGNMENT.md`, `BROKERS.md` + `brokers/<id>.md` (per-broker research, UNVERIFIED markers),
  `LIMITATIONS.md`, `ADDING_A_BROKER.md`, `HANDOFF.md`, `progress.md`, `beads.toml` (task graph),
  `samples/` (rebalance.json/.csv), `img/` (UI screenshots), `PROMPTS.md`.
- `scripts/`: `beads.py` (task tracker), `demo.sh` (README commands end to end), `ui_smoke.py`
  (UI API flows + samples + screenshots), `spike.py` (throwaway).
- `.claude/` (agents, slash commands: next-bead, gate, handoff, resume, audit) and `CLAUDE.md`
  (project rules, workflow) support the Claude Code build workflow.

## 3. Architecture and non-negotiable rules
Flow: API validates synchronously (V1-V11, whole request rejected, nothing persisted or sent) ->
run + legs inserted (`PLANNED`, SELL phase then BUY phase) -> executor claims a DB run lease ->
per leg compare-and-set `PLANNED->SUBMITTING` committed **before** the broker call (write-ahead) ->
rate limiter -> `place_order` -> poll to a final state -> barrier (every sell terminal, UNKNOWN or
past timeout before any buy) -> finalise -> outbox row in the same transaction -> signed webhook.
- **Never blindly retry `place_order`.** Only RETRY_SAFE errors retry (429, connect error before
  send). Ambiguous outcomes (timeout, 5xx, garbled reply after send) are reconciled by deterministic
  order tag (`K` + base32(sha1(run_id:leg_index)), length <= broker tag limit, 8..20); a miss means
  leg `UNKNOWN` (rechecked at +60 s), never resubmitted (D9, D23, D29).
- Unknown broker status strings are non-terminal, never `FILLED` (D19). `sellable_qty` is the most
  conservative field (D26). Conflicting rate limits: use the lowest; tag limits: the shorter.
- Idempotency: `Idempotency-Key` required; same key + body returns the same run, other body 422.
- Crash recovery: lease + fencing on every write; a resume sweep re-claims expired runs; one worker
  only (`--workers 1`, D20). Auth expiry mid-run stops placing (D32).
- Secrets: broker tokens Fernet-encrypted at rest; passwords/TOTP/PIN never persisted; JSON logs
  redact secrets (`logging.py`, D45); the API never returns tokens.
- A broker = ONE file in `brokers/` + ONE fixture; no core edit; no broker imports another; no
  official SDK order calls (D25); async only; domain has no `Any`, no I/O.
- Dependency rule: `domain` <- `planner` <- `execution`/`api`; keep modules < 300 lines (adapters <= 200).

## 4. API surface and contracts (details in `docs/SPEC.md`)
- Auth to our API: `X-API-Key` (`API_KEYS=user:key[,user:key]`; send the part after the colon).
  Public: `GET /v1/brokers`, `/healthz`, `/readyz`, `/ui`, `/ui/portfolio.js`, `/mock/webhook`
  (HMAC-verified POST; GET lists recent), OAuth callback (authenticated by one-time `state`).
- `GET /v1/sessions/login-url?broker=` (OAuth brokers), `GET /v1/sessions/callback`,
  `POST /v1/sessions {broker, credentials}` (non-OAuth, Paper; Paper `credentials.profile="demo"`
  seeds RELIANCE 10, TCS 5, INFY 8, ITC 40), `POST /v1/executions/preview`,
  `POST /v1/executions` (202 + run_id), `GET /v1/executions/{id}` (legs, events, notifications incl.
  payload), `GET /v1/executions?limit=`.
- Body: `{session_id, mode: FIRST_TIME|REBALANCE, options{order_type, product CNC, exchange,
  halt_on_sell_failure, poll_timeout_s, webhook_url}, instructions[{symbol, action BUY|SELL|REBALANCE,
  side?, quantity, order_type?, limit_price?}]}`. FIRST_TIME needs empty holdings (409 otherwise).
- Run status: CREATED -> RUNNING -> COMPLETED | COMPLETED_WITH_FAILURES | FAILED. Leg status:
  PLANNED, SUBMITTING, SUBMITTED, OPEN, PARTIAL, FILLED, REJECTED, CANCELLED, FAILED, UNKNOWN, SKIPPED.
- Webhook (signed `X-Kalpi-Signature: sha256=<hmac>`): `event` (`execution.completed` or
  `execution.updated`), `run_id`, `mode`, `broker`, `status`, `summary{counts}`, `executed[]`,
  `failed[]`, `seq`. Outbox retries 1, 2, 4, 8, 16 s then dead-letters.
- Errors: `{"error": {"code", "message", "details"}}`. `/readyz` is 503 if the DB is down or no broker
  is registered. Logs are JSON with `run_id`, `leg_id`, `request_id`; response header `X-Request-ID`.

## 5. Brokers (all mocked-HTTP only; see `docs/brokers/<id>.md`, `docs/LIMITATIONS.md`)
| id | auth | notes / known gaps |
|---|---|---|
| paper | none | simulated account, fault injection; first-class, selectable in UI |
| zerodha | OAuth redirect, `request_token` + sha256 checksum | MARKET sends `market_protection=-1`; base host, auth header, margins field UNVERIFIED; static IP for orders |
| upstox | OAuth code | MARKET `market_protection=-1`, `slice=false`; token content type, holdings qty semantics UNVERIFIED |
| fyers | OAuth, `appIdHash` | numeric order status codes undocumented: no terminal mapping, legs end UNKNOWN, never FILLED; no MPP param |
| angelone | client code + PIN + TOTP (computed in-process) | needs `ANGELONE_CLIENT_LOCAL_IP/PUBLIC_IP/MAC` or login errors; MARKET price "0"; product code UNVERIFIED |
| groww | key+secret checksum, TOTP, or pasted token | only HTTP 401 maps to auth expiry; no MPP param |
Regulatory facts (static IP for orders, daily 2FA, ~10 orders/s, market->MPP) are in each adapter's
`meta` and `docs/BROKERS.md`. Credentials come from env (names in `.env.example`) via constructor
defaults; none of the five has had a real call.

## 6. Running, testing, conventions
- Setup: `cp .env.example .env`; generate `FERNET_KEY` with
  `python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`; keep
  `API_KEYS` (default key `change-me-demo-key`); `docker compose up --build -d`; open `/ui`.
  Env: DATABASE_URL, FERNET_KEY, API_KEYS, WEBHOOK_SECRET, DEFAULT_WEBHOOK_URL, MAX_QTY_PER_ORDER,
  MAX_CONCURRENCY, LEASE_TTL_S, POLL_INTERVAL_S, RECHECK_INTERVAL_S, LOG_LEVEL, plus broker creds.
- `make check` = ruff + mypy(domain) + pytest (392 tests, ~4 min). `make itest` = compose + Postgres
  integration (3 tests). `bash scripts/demo.sh`, `uv run python scripts/ui_smoke.py` (needs local
  Chrome for screenshots, node for CSV parsing). Live tests are `-m live` and are never run unless asked.
- **Compose safety**: the project name is the directory name, so a same-named clone shares
  containers and the Postgres volume. For any second copy use
  `APP_PORT=<free> docker compose -p <other-name> ...`. Never `docker compose down -v` on the
  owner's `kalpi-engine` project; never read, print or commit `.env` or any token.
- Style: ruff line length 100, small modules, `# fmt: skip` packed lines exist, so avoid running
  `ruff format` on adapters. Tests mirror the layout; use Paper + fault injection for engine
  tests, respx for adapters. Commit per task as `feat|fix|test|docs(<id>): summary`. Windows host
  (Git Bash); CRLF warnings on commit are harmless.
- Decisions to know: D1/D25 own httpx adapters, no SDK or OpenAlgo; D6 no delta calc; D7/D21
  sells-barrier-buys; D20/D30 lease; D35 instrument master loaded once at startup; D37 minimal UI;
  D40 `/v1/brokers` public; D41 OAuth state in memory; D45 stdlib JSON logging.

## 7. Next steps (priority order)
1. **Live-test the other adapters** (upstox, fyers, angelone, groww; inferred item, the original
   brief was cut off): one broker at a time, read-only first, record results in
   `docs/brokers/<id>.md`, flip `live_tested` only for what a real call proved, update the README
   Live-tested table and the UI badge.
2. **Zerodha live integration**: read-only first (OAuth login via `/v1/sessions/login-url`, token
   exchange, holdings, funds); then, only if the owner agrees and the market is open, one-share order
   plus status and tag lookup. Constraints: static IP whitelist for order endpoints, daily login
   (token expires ~06:00 IST), redirect URL fixed in the Kite console (the `state` round trip via
   `redirect_params` is UNVERIFIED). Record results in `docs/brokers/zerodha.md` and a README
   "Live-tested" table (does not exist yet; create it). Never commit tokens, `.env`, or raw responses with secrets.
3. **UI polish**: clearer states and errors, run history list, copy button for the webhook payload,
   regenerate `docs/img/` with `scripts/ui_smoke.py`, accessibility basics (labels, focus, contrast,
   keyboard use).
4. Backlog: B8 adaptive concurrency halving after repeated 429 (README lists it as not built);
   S1 TARGET_STATE mode (delta); S2 SSE run events; S3 Alembic migrations.

## 8. Problem Statement 2 (future, separate task)
Kalpi Builder Assignment v2, Problem 2: a **Multi-Frequency Financial Data Platform design document**
(1000-1500 words: point-in-time correctness, tick storage, on-write / on-read / scheduled compute,
serving). Not started, out of scope for this repo (`docs/ASSIGNMENT.md` lines 26 and 46-47;
`docs/PROMPTS.md` placeholder; `START-HERE.md` step 7). Run it as its own session with its own
plan: when it starts, begin with a plan and `/audit plan`. The assignment's evaluation lens
(inferred from Problem 2's criteria) is in `docs/ASSIGNMENT.md` and is worth re-reading for it. Do not mix it with engine changes.

## 9. Gotchas learned
- Fyers legs never reach FILLED (status vocabulary unknown); do not "fix" this without a recorded
  live response.
- Headless Chrome `--virtual-time-budget` races `File.text()`; poll instead of fixed timeouts.
- The UI's default API key is `change-me-demo-key`; Paper with `profile=demo` is needed for REBALANCE
  demos, none for FIRST_TIME (UI reconnects Paper when the mode changes).
- Subagents given repo access must be told not to touch the owner's compose stack or `.env`.
