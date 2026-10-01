# START HERE — Kalpi Execution Engine kit

What this is: a repo-ready plan + Claude Code setup for **Problem 1 (Portfolio Trade Execution Engine)**. Method: the "Build a Perfect Plan" workflow (plan-only mode, primary-source research, prompt log, dependency graph with measurable done-when, decision gates, named failure scenarios, adversarial review, state in files) adapted for a 24h build.

## What's inside
| Path | Purpose |
|---|---|
| `CLAUDE.md` | <80-line project memory: session protocol, money-safety rules, token discipline |
| `docs/ASSIGNMENT.md` | R1–R12 requirement checklist, spec ambiguities + resolutions, questions for Kalpi |
| `docs/DECISIONS.md` | D1–D17 with alternatives and trade-offs (you must be able to defend these) |
| `docs/SPEC.md` | API, payloads, validation V1–V10, state machines, adapter port, algorithm, tables |
| `docs/PLAN.md` | Schedule, gates G0–G4, failure scenarios F1–F12, invariants I1–I7, review log |
| `docs/beads.toml` + `scripts/beads.py` | 33-bead dependency graph and a stdlib CLI (`ready/show/start/close/lint/status`) so Claude never loads the whole plan |
| `docs/BROKERS.md` | Broker research ledger (what's known, what's UNVERIFIED) |
| `docs/PROMPTS.md`, `progress.md`, `HANDOFF.md` | Requirement log, auto progress trail, session handoff |
| `.claude/commands/` | `/next-bead` `/gate` `/handoff` `/resume` `/audit` |
| `.claude/agents/` | `plan-critic` (opus), `broker-researcher` (sonnet), `verifier` (sonnet) |
| `.claude/settings.json` | Pre-approved safe commands; denies `.env`, force-push, hard reset |

## Honest caveats
- Broker endpoint details are **not yet verified**. Bead A1 does that from primary docs; until then treat DECISIONS D13 identifiers as hypotheses.
- Live trading needs per-broker app registration, funded account, static IP and daily 2FA (SEBI retail algo framework as reported by Fyers). Reviewers cannot run live either, so the demo runs on the Paper broker and the README must say exactly what was live-tested.
- Time estimates are agent-time; plan on parallelism (critical path 11.5 h vs 21 h sequential).

## Steps

### 0 · Prerequisites (10 min)
Claude Code installed and logged in · git · Docker + Compose · `uv` (https://docs.astral.sh/uv/) · empty GitHub repo (keep **private** until G4, make public at submission).

### 1 · Create the repo (5 min)
```bash
unzip kalpi-engine-kit.zip && cd kalpi-engine
git init && git add -A && git commit -m "docs: starter kit (plan v0)"
git branch -M main && git remote add origin <your-private-repo-url> && git push -u origin main
python3 scripts/beads.py lint      # expect: OK: 33 beads ... no cycles
python3 scripts/beads.py ready     # expect: A1, A2
```

### 2 · Plan freeze (60–90 min) — in Claude Code, Plan Mode, opus (`/model`)
1. **Start research first (background, ~40 min):**
   *"Read CLAUDE.md. Do bead A1: spawn five broker-researcher subagents in parallel (zerodha, upstox, fyers, angelone, groww). When all return, fill the comparison table in docs/BROKERS.md using their files and close A1 with the UNVERIFIED count as evidence."*
2. **Read DECISIONS D1, D3, D6, D7, D9 yourself.** Change anything you disagree with (append a row; don't silently edit). You will be asked to explain these.
3. **Adversarial review:** *"Use the plan-critic agent on the plan."* For each critique decide adopt/reject **with a reason**, record in the review-log table in `docs/PLAN.md`, edit beads/SPEC accordingly.
4. **Prompt audit:** add any extra requirements of yours to `docs/PROMPTS.md`, then `/audit plan`.
5. *(Optional, 2 min)* email info@kalpicapital.com the three questions at the bottom of `docs/ASSIGNMENT.md`. Don't wait for a reply.
6. `python3 scripts/beads.py lint` → `git commit -am "docs: plan v1 frozen"`. **Rule: after this, plan changes are logged, not improvised.**

### 3 · Build loop (switch to sonnet; opus only for B4 design and gates)
- Per bead: `/next-bead` (it picks, builds to `done_when`, verifies, closes with evidence, commits, prints one line).
- At every gate: `/gate` → if PASS: `/handoff`, then `/clear`, then `/resume`. If ADJUST/ABORT: Claude stops and tells you; you decide.
- Phase A → **G0** → Phase B → **G1** (hard stop if any duplicate order appears) → Phase C → **G2**.

### 4 · Phase C in parallel (the biggest time saver)
After C0 and A1 are closed:
*"Do beads C1–C5 in parallel: spawn five general-purpose subagents, one per broker. Each may only create brokers/<id>.py and tests/contract/fixtures/<id>.py, must follow docs/brokers/<id>.md, and returns <=120 words. Then you run the contract suite, and close each bead with evidence."*
Run D1 (UI) and E1 in a second Claude Code terminal at the same time using `git worktree add ../kalpi-ui -b ui` (merge when done; `docs/state.json` and `progress.md` are closed from the main tree only, to avoid conflicts).

### 5 · Release (H11–H16)
E2 integration test → E3 README (must contain: Docker commands, architecture + rebalance walk-through, library justification, live-tested vs mocked, regulatory notes) → E4 clean-room dry run (fresh clone) → E5 `/audit release` → G4 → make the repo public → submit the link.

### 6 · Use the buffer (H16–24)
- One real **live smoke test** on the broker whose API access you already have (cheapest/free). 1 share, record the result in README. Needs static IP + fresh daily login.
- Fix `WEAK` audit items. Rehearse explaining D1, D9, D10 aloud (the brief says you must explain mechanics).
- Do **not** start new scope after H20.

### 7 · Problem 2 (later, separate session)
Design doc, 1000–1500 words. Same method in a new folder: prompts log → critic → write. Not in this repo.

## Token-efficiency checklist
- [ ] Sonnet for building; opus only for plan critique, B4, gates
- [ ] `/clear` at every gate; `/resume` costs ~2k tokens
- [ ] Heavy reading (docs, logs, failing tests) goes to subagents that return <=200 words
- [ ] Tests: `-q -x --tb=short | tail -30`; never paste full logs
- [ ] One bead per turn; Claude prints one line when done
- [ ] State in `docs/state.json`, `progress.md`, `HANDOFF.md` — not in chat

## Red flags to watch for while Claude builds
- A bead closed with vague evidence ("works") instead of numbers
- Any retry wrapper around `place_order`, or any `place_order` call outside the executor
- A broker adapter that imports another adapter or edits core files
- Tokens/TOTP/PIN in logs, DB rows, or test fixtures
- Claude "improving" the plan mid-build without a PLAN/DECISIONS entry
