# PROMPTS — verbatim requirement log (audit this before freezing the plan)

Rule: append every instruction from the human here, with the decisions it implies. At plan freeze, `/audit` checks each prompt against the beads.

### Prompt 1 — Mission & workflow
> Starting a new backend project for a systematic quant investing platform based out of India. Set up a workflow for Claude Code to get through it within 24 hours, most efficient in tokens and time, giving a quality product. Plan -> Build (all phases planned even for further projects). Follow the "How to Build a Perfect Plan" workflow.

**Decisions:** plan-first; token efficiency is a hard constraint; plan covers all phases; beads graph + gates + failure scenarios + adversarial review + progress-in-files.

### Prompt 2 — Task + scope
> This is our task at hand (Kalpi Builder Assignment v2). Analyse patiently, tradeoffs etc. Go with Problem Statement 1 first. Get the starter kit done, then give steps to proceed.

**Decisions:** Problem 1 only (Portfolio Trade Execution Engine); Problem 2 (data platform design doc) is a later, separate session; starter kit = Claude Code config + plan v0; decisions with explicit trade-offs.

### Prompt 3 — Plan critique, read-only
> Use the plan-critic agent to review the plan. Show its critiques as a table (#, severity, evidence, proposed change). Do NOT edit any file yet.

**Decisions:** adversarial review before freeze; no edits without human approval.

### Prompt 4 — Round 1 rulings (12 critiques)
> Accept all 12 with two amendments:
> - #5: do not hard-reject MARKET for Groww. Add meta.market_order_verified (false when MPP handling is UNVERIFIED); preview shows a warning and the contract fixture asserts the outgoing payload.
> - #6: also amend F1 step 2 and D1: never wrap the SDK's place_order; at most copy SDK constants.
> - #3: change G0's done_when to "Gate table filled with measured values; verdict PASS, or ADJUST with the F1 actions recorded per broker; never ABORT". Reclassify Fyers per D18 (3 blocking = ADJUST). The G0 verdict itself is only recorded by /gate after A5 exists, so remove any premature A5/elapsed-time verdict.

**Decisions:** D20–D29. G0 rules: PASS or ADJUST-with-F1-actions closes G0, never ABORT; 3 blocking items = ADJUST (so Upstox is ADJUST too); verdict only via `/gate` after A5. Groww MARKET warns, never hard-rejects (D24). Never wrap an SDK's `place_order` (D25).

### Prompt 5 — Zerodha note fix
> Fix zerodha.md line 19 too, then commit.

**Decisions:** `docs/brokers/zerodha.md` copies SDK constants only, no SDK wrapping (D25). Done in commit 0dae99e.

### Ruling (human, 2026-10-01, before this log entry) — Zerodha G0
**Decisions:** Zerodha G0 = ADJUST (F1 steps 2–3), not re-researched (recorded in PLAN G0 pre-assessment and BROKERS.md triage).

### Prompt 6 — Round 2 rulings (10 critiques, final review round)
> Adopt all 10 and apply them, including the #22 scope cuts (instrument master refreshed at startup only; adaptive concurrency halving becomes a P1 follow-up bead). Keep #15 minimal: a recheck_at column and a simple integer seq on events, nothing more. [...] This is the FINAL plan-review round.

**Decisions:** D30–D35; B8 (P1) created; plan v1 frozen; no further plan-review rounds.

### Problem 2 — placeholder (later, separate session)
Scope: Kalpi Builder Assignment v2, Problem 2 (data platform design doc). **Not planned in this repo/session** (Prompt 2). When started: new session, own PLAN/beads, run `/audit plan` before building. No beads exist for it yet, by decision.

### Prompt 7 — (append here)
