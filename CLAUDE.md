# Kalpi Execution Engine — project memory (keep <80 lines; loaded every session)

Goal: Portfolio Trade Execution Engine for Kalpi's Builder assignment (Problem 1). FastAPI + Docker. 5 broker adapters + Paper broker. 24h budget.
Owner: Milind. Plan: `docs/PLAN.md`. Tasks: `docs/beads.toml`. Contracts: `docs/SPEC.md`. Decisions: `docs/DECISIONS.md`. Requirements: `docs/ASSIGNMENT.md`.

## Session start protocol (always)
1. `python scripts/beads.py status` and `python scripts/beads.py ready`. If resuming: read `docs/HANDOFF.md` (<=15 lines) and `git log -5 --oneline`. Do NOT re-read the whole plan.
2. Work ONE bead at a time: `python scripts/beads.py show <id>` -> read ONLY the SPEC/DECISIONS section it names (`grep -n '^## ' docs/SPEC.md`, then read that range).
3. `python scripts/beads.py start <id>` -> build -> verify its `done_when` with a command -> `python scripts/beads.py close <id> -e "<measured evidence>"` -> `git commit -m "feat(<id>): ..."`.
4. At a gate bead: run `/gate`. If verdict is ADJUST/ABORT, STOP and tell the human. Never continue past a failed gate.

## Non-negotiables (money safety)
- NEVER blindly retry `place_order`. Only RETRY_SAFE errors (429, connect-before-send) retry. Ambiguous (timeout/5xx after send) -> reconcile by tag; never auto-resubmit (DECISIONS D9).
- Commit leg state `SUBMITTING` BEFORE the broker call (write-ahead).
- Sells phase fully terminal before buys phase starts (unless halted).
- Validation failures reject the WHOLE request; nothing is sent to the broker.
- Never log or persist tokens, passwords, TOTP, API secrets (hash/redact). Tokens encrypted at rest (Fernet).
- Never claim an adapter is live-tested unless a real call succeeded; mark `experimental`/`UNVERIFIED` honestly.
- Never loosen a bead's `done_when` silently. 3 failed attempts -> split the bead + log + ask human (failure F11).

## Architecture rules
- Layout in SPEC §1. `domain/` imports nothing internal and does no I/O. `planner/` is pure functions.
- A broker = ONE file in `src/kalpi_engine/brokers/` implementing `BrokerAdapter`; auto-discovered. No broker imports another broker. No core edits to add a broker.
- Async everywhere (httpx.AsyncClient, SQLAlchemy async). No blocking calls in the event loop.
- Errors use the taxonomy in `domain/errors.py`; API errors use the envelope in SPEC §2.
- Type hints on public functions; pydantic v2 at boundaries; no `Any` in domain.

## Commands
- `make check` = ruff + mypy(domain) + pytest -q ; `make test` ; `make itest` (compose stack) ; `make up` / `make down`
- `uv run pytest -q -x --tb=short tests/<area>` ; `uv run ruff check --output-format=concise`
- Never run live-broker tests unless explicitly asked (`-m live`).

## Token & context discipline
- Don't read files >200 lines whole; use grep/line ranges. Don't print full test/log output: tail or summarise (`| tail -30`).
- Delegate heavy reading/analysis to subagents with a <=200-word return: `broker-researcher`, `plan-critic`, `verifier`.
- Prefer Edit over rewriting files. No speculative abstractions, no extra features outside the current bead.
- Progress goes to files (`progress.md` auto, `HANDOFF.md` via `/handoff`), not chat. `/clear` between phases; `/resume` after.
- Model: opus for plan critique, B4 design, gates; sonnet for building; haiku for log summaries.

## Style
- Python 3.12, ruff defaults + line length 100, small modules (<300 lines), docstring only where behaviour is non-obvious.
- Tests mirror layout under `tests/`; use the Paper broker + fault injection for engine tests; respx for adapters.
- Commit per bead; message `feat|fix|test|docs(<bead>): summary`.

## Slash commands
`/next-bead [id]` · `/gate [Gx]` · `/handoff` · `/resume` · `/audit`
