# PLAN v1 — Portfolio Trade Execution Engine (status: FROZEN 2026-10-01; plan-critic: 2 rounds applied)

Task graph lives in `docs/beads.toml` (use `python scripts/beads.py ready|show|close`). This file holds the parts that are not tasks: gates, failure scenarios, insight tiers, invariants, schedule. Decisions: `docs/DECISIONS.md`. Contracts: `docs/SPEC.md`.

## Numbers
- 36 beads = 28 tasks + 5 gates + 3 stretch. Non-stretch effort ≈ 1565 min (gates included); P0 ≈ 1415 min. B8 (P1) runs after G1 only if time allows.
- **Critical path 695 min (11.6 h)** if independent beads run in parallel: A2→A3→A4→A5→G0→B2→B4a→B4b→B4c→B6→B7→G1→C6→G2→E3→E4→E5→G4.
- Adapters are now off the critical path: C0 starts after A4, C1–C5 after C0+G0, in parallel with phase B; G1 still gates their merge (C6).
- Sequential execution would be ~26 h: **parallelism is mandatory** (A1 research ‖ A2–A5; B1 ‖ B2 ‖ B3; C0→C1–C5 five-way ‖ phase B; D1 ‖ E1 after G1).

## Schedule (H = hours from start; target ship at H16, H16–24 is buffer)
| Window | Work | Exit |
|---|---|---|
| H0:00–1:30 | Review kit: `plan-critic`, `/audit` of PROMPTS, edit, commit. **Start A1 research in background at H0:15.** | Plan frozen + committed |
| H1:30–4:30 | A2→A5 (A1 finishing in parallel) | **G0** |
| H4:30–10:30 | B1‖B2‖B3 → B5 → B4a (opus design, then build) → B4b → B4c → B6 → B7. In parallel: C0 (from ~H2:30) → C1…C5 (worktrees/subagents) | **G1** |
| H10:30–12:00 | C6 → G2 ‖ D1 ‖ E1 ‖ E2 | **G2, G3** |
| H12:00–16:00 | E3 README → E4 clean-room → E5 audit | **G4** (push public repo) |
| H15:30–24:00 | Buffer: live smoke on one real broker, fix WEAK items, S1 if time. **Do not start new scope after H20.** | Submit |

## Gates (Pass / Adjust / Abort — measured values only; "looks good" is not a gate)
**G0 Feasibility** (after A1+A5)
| Check | Pass | Adjust | Abort/escalate |
|---|---|---|---|
| A5 spike prints 5 FILLED legs via the real port | yes, <5 s | works but port needed changes -> update SPEC §4 then continue | cannot express Paper through the port -> redesign port before anything else |
| BROKERS.md BLOCKING UNVERIFIED items per broker (D18, D22) | <=2 each | 3–6 for a broker -> apply F1 cascade to that broker | >=3 brokers with >6 -> F1 step 4 (flag experimental, tell human) |
| Elapsed since start (stale after G0; pace now vs 13:45 deadline, D42) | <=H4:30 | H4:30–6:00 -> cut D1 to minimal (F12) | >H6:00 -> drop S*, drop E1 detail, tell human |

G0 closes on PASS, or ADJUST with the F1 actions recorded per broker; never ABORT (D22). The verdict is recorded only by `/gate` after A5 exists.

Pre-assessment of the UNVERIFIED-items row only (A1, 2026-10-01; not a gate verdict): ADJUST for all five, i.e. fyers (3 blocking), upstox (3), zerodha (4), groww (4) and angelone (5). F1 actions: step 2 = copy constants/enums from the official SDK source, **never wrap the SDK's `place_order`** (D25); step 3 = ship `experimental=true` with contract tests + README disclosure and the D19 defensive defaults, unless live-tested. Zerodha ruled ADJUST by human 2026-10-01 (not re-researched).

All gates (D33): PASS, or ADJUST with that row's actions done and recorded, closes the gate; ABORT stops and escalates.

**G1 Engine correct on Paper** (after B7)
| Check | Pass | Adjust | Abort |
|---|---|---|---|
| Invariants I1–I7 over 20 seeds | 20/20 | 18–19 -> fix + rerun; max 2 loops | <18 after 2 loops -> stop, re-read SPEC §5, escalate |
| Planner coverage | >=95% | 85–94% -> add cases | <85% |
| Duplicate orders at Paper | 0 | — | any >0 -> **hard stop**, fix before any adapter work |

**G2 Adapters** (after C6)
| Check | Pass | Adjust | Abort |
|---|---|---|---|
| Contract suite green per adapter | 5/5 | 4/5 -> fix, or list the red adapter under README Limitations as contract-failing | <=3/5 -> F1.4 and tell human immediately |
| Sixth-broker proof | 1 file, 0 other edits | 2–3 files -> refactor port | requires core edits -> redesign registry |
| Adapter size | each <=200 LOC | 200–300 | >300 -> push logic into base class |

**G3 UI smoke** (after D1)
| Check | Pass | Adjust | Abort |
|---|---|---|---|
| First-time + rebalance flows on Paper | both complete, 0 console errors | cosmetic bugs only | flow broken >45 min -> F12 minimal UI |

**G4 Release** (after E5)
| Check | Pass | Adjust | Abort |
|---|---|---|---|
| Fresh clone `docker compose up --build` healthy | <=3 min | 3–6 min -> slim image/caching | fails -> F10 |
| `make check` + `make itest` | green | flaky test -> fix root cause | red |
| Audit R1–R12 | 0 MISSING | WEAK only -> list in README Limitations | any MISSING -> fix first |
| Public repo clones & runs from README alone | yes | typo fixes | no |

## Failure scenarios (numbered cascades: try cheapest first)
| ID | Failure | Detect | Recovery cascade |
|---|---|---|---|
| F1 | Broker API can't be verified or adapter infeasible | A1 UNVERIFIED count; contract test can't be written | 1) re-run researcher using official docs + reading the official SDK source as reference · 2) copy constants/enums/paths from the official SDK into our httpx adapter; **never wrap the SDK's `place_order`** (D25) · 3) ship adapter `experimental=true` with contract tests + README disclosure · 4) fewer documented adapters + Paper, tell the human; **never claim live-tested** |
| F2 | 429 / rate-limit storm | `RateLimited` events | 1) honour `Retry-After` · 2) jittered backoff · 3) halve concurrency for the session (P1, B8) · 4) after 5 attempts mark leg `FAILED(RATE_LIMITED)`, continue others |
| F3 | Ambiguous submit (risk of duplicate order) | timeout/5xx after send | 1) `find_order_by_tag` ×3 with backoff · 2) `UNKNOWN`, **no resubmit**; if the broker lacks tag lookup, list order-book candidates (symbol+side+qty+time window) as `possible_matches` in the notification, **report-only, never adopted** (D29) · 3) re-check by tag at T+60s and at finalise; a late hit updates the leg and emits `execution.updated` (D21) |
| F4 | Session expired mid-run | `AuthExpired` | 1) stop placing · 2) remaining legs `SKIPPED(AUTH_EXPIRED)` · 3) notify; user reconnects and starts a new run with a new key (resume endpoint is stretch) |
| F5 | Insufficient funds / RMS reject | broker reject text | 1) leg `REJECTED(reason)` · 2) continue others · 3) preview warns if `get_funds` supported |
| F6 | Order still OPEN at poll timeout | poll timeout | leave `OPEN`, report, never auto-cancel; an OPEN sell counts as a sell failure at the barrier (D21) |
| F7 | Process crash mid-run | startup scan of non-terminal runs | 0) claim the run lease (startup + periodic sweep); skip if another worker holds it (D20, D30) · 1) reconcile `SUBMITTING` legs by tag (miss -> `UNKNOWN`, never resend) · 2) continue only `PLANNED` legs · 3) `UNKNOWN` for unresolvable |
| F8 | Webhook consumer down | delivery errors | 1) outbox retry 1,2,4,8,16 s · 2) dead-letter flag · 3) result still on `GET /v1/executions/{id}` |
| F9 | Market closed / holiday | V10 warning; broker reject | warn in preview; reject reported as `REJECTED(MARKET_CLOSED)`; no AMO in v1 |
| F10 | Docker/compose breakage | `make up` / E4 fails | 1) healthchecks + `depends_on: service_healthy` · 2) pin base image/package versions · 3) SQLite profile for demo, Postgres documented |
| F11 | A bead fails its done_when 3 times / scope creep | 3 failed attempts | split the bead, log in progress.md, ask the human; **never loosen done_when silently** |
| F12 | UI overruns | G3 clock | minimal single page (JSON textarea + 3 buttons) or Swagger UI + curl script; disclose bonus partially done |

## Insight tiers (higher tier wins conflicts)
- **Tier 1 — plan fails without:** never double-order (D9) · real adapter port with 5 adapters + Paper (R1/R2) · correct rebalance semantics with sells before buys (R5) · notification lists executed AND failed (R6) · `docker compose up` works from a clean clone (R8) · honest README with the 3 required sections (R11).
- **Tier 2 — significantly affects quality:** rate limiting + backoff (R9) · audit events · preview endpoint · token encryption · contract suite · UI (R10) · instrument resolver.
- **Tier 3 — incremental:** TARGET_STATE (S1) · SSE (S2) · Alembic (S3) · resume endpoint.
- **Does not apply:** Celery/Kafka, market-data streaming, portfolio optimisation, tax-lot/charges maths, derivatives/GTT, multi-tenant billing.

## Invariants (asserted by B7 after every randomised run)
I1 at most one broker order per leg tag (incl. two concurrent resumers and a lease lost mid-`place_order`) · I2 no leg left in `PLANNED`/`SUBMITTING`/`SUBMITTED` at run end · I3 no buy leg submitted before every sell is resolved (terminal, `UNKNOWN` or timed out); `UNKNOWN`/`OPEN`/`PARTIAL` sells count as sell failures (D21) · I4 notification lists every non-filled leg (PARTIAL in both `executed` and `failed`); per-status counts add up to total · I5 no secret in logs/events · I6 same Idempotency-Key + same body → same run, no extra orders; different body → 422 · I7 run status is a pure function of leg statuses per the SPEC §3 table · I8 a run stranded in `SUBMITTING` by an app restart is resumed by the lifespan resume/recheck sweeps (adopt by tag or `UNKNOWN`), never resent.

## Execution strategy (token efficiency + compaction survival)
1. State lives in files: `docs/state.json` (bead status), `docs/progress.md` (auto-appended on close), `docs/HANDOFF.md` (<=15 lines, via `/handoff`). Conversation is disposable.
2. Never read whole docs: `beads.py show <id>` gives the bead plus the exact SPEC section to read.
3. Heavy reading/output goes to subagents returning <=200 words (broker research, log triage, test failure triage).
4. Model split: opus for plan critique, B4a design and gate verdicts; sonnet for building; haiku acceptable for log summarising.
5. Commit per bead (`feat(B4): ...`). Green commit hash goes into the bead evidence.
6. `/clear` at each gate. `/resume` restores context in ~2k tokens.
7. Test output: `pytest -q -x --tb=short`, `ruff check --output-format=concise`; never paste full logs back into the chat.

## Adversarial review log (fill after running the `plan-critic` agent)
Review run 2026-10-01 (plan-critic, 12 critiques). All 12 adopted, two with human amendments.

| # | Critique | Verdict (adopt / reject) | Reason | Plan change |
|---|---|---|---|---|
| 1 | BLOCKER: crash-resume has no run claim, so two workers could double-send | adopt | money safety | D20 lease (`lease_owner`/`lease_until`, SQLite + Postgres) + leg CAS + `--workers 1`; SPEC §5.3/§7; B2, B4c race test, A2 |
| 2 | BLOCKER: UNKNOWN/OPEN/PARTIAL sells let buys run | adopt | money safety | D21; SPEC §3/§5.7–8; late check by tag at T+60s and at finalise; B4a, B4b, B7 late-fill fault; I3 |
| 3 | BLOCKER: G0 requires PASS but was ruled ADJUST, so the plan deadlocks | adopt | unblock honestly | D22; G0 `done_when` = PASS or ADJUST with F1 actions, never ABORT; threshold <=2 PASS, so fyers and upstox (3) are ADJUST; premature verdict text removed |
| 4 | HIGH: RETRY_SAFE underspecified at the transport layer | adopt | classification is the crux | D23 table in SPEC §4; A4 one test per class |
| 5 | HIGH: adapters would reject MARKET; Upstox returns `order_ids[]` | adopt, **amended** | human: no hard reject for Groww | D24 `meta.market_order_verified` (false for Groww), preview warning V11, fixtures assert payloads; `slice=false`, `order_ids[0]`; C1–C5 |
| 6 | HIGH: wrapping the SDK breaks money safety | adopt, **amended** | human: also amend F1 step 2 and D1 | D25 never wrap the SDK's `place_order`, at most copy constants; F1, D1 fallback text, C1–C5 grep check |
| 7 | HIGH: sellable quantity has nowhere to live | adopt | V4 correctness | D26 `Holding.sellable_qty`; SPEC §3, V4; A3, B1, C1–C5 |
| 8 | HIGH: idempotency vs validation ordering undefined | adopt | API contract | D27 validate before insert, `request_hash`, 422 `IDEMPOTENCY_KEY_REUSED`; SPEC §2/§5/§7; B2, B6 |
| 9 | MEDIUM: run/notification state machine has holes | adopt | I4/I7 testable | D28 leg-to-run table, `partial` and per-status counts; SPEC §3/§6; A3, B5 |
| 10 | MEDIUM: dependency errors | adopt | shorter path | E3←G2 only; E2 drops E1; C0←A4; C1–C5←C0+A1+G0; C6←G1 (G1 still gates merge) |
| 11 | MEDIUM: fat or vague beads | adopt | F11 prevention | B4→B4a/B4b/B4c; C1–C5 est 90; C0 resolver base + login/checksum/payload fixtures; D1/E3 scripted `done_when` |
| 12 | MEDIUM: `experimental` not wired; F3 heuristic contradicts D9 | adopt | honesty + money safety | D29 `experimental`/`live_tested` in `BrokerMeta` and `GET /v1/brokers`; F3 heuristic report-only (`possible_matches`) |

Review run 2 2026-10-01 (`/audit plan`, plan-critic second pass, 10 critiques). All 10 adopted by the human; #15 kept minimal (`recheck_at` + integer `seq` only). This was the final plan-review round.

| # | Critique | Verdict (adopt / reject) | Reason | Plan change |
|---|---|---|---|---|
| 13 | BLOCKER: lease does not fence writes; a tag miss on resumed `SUBMITTING` could resend | adopt | money safety; resend contradicts D9 | tag miss on `SUBMITTING` -> `UNKNOWN`, never resend; every leg/run UPDATE adds `AND runs.lease_owner=:me`; renew in its own task; B4c cases |
| 14 | HIGH: a restart inside `LEASE_TTL_S` orphans the run (startup-only scan) | adopt | liveness | periodic sweeper re-claims after `lease_until`; B4c test |
| 15 | HIGH: late reconcile is in-memory and races the finalise check | adopt | durability + no double adoption | D31: `legs.recheck_at` swept by worker, CAS on status; integer `events.seq`, latest in payload; nothing more (human) |
| 16 | HIGH: B4b needs B5's outbox; B4a tests UNKNOWN sells before B4b creates UNKNOWN | adopt | dependency correctness | B4b blocked_by += B5; move the UNKNOWN-barrier case to B4b |
| 17 | HIGH: R1 authentication untested at API level | adopt | R1 coverage | B6 done_when: login-url/callback (state/CSRF check, encrypted store, `expires_at`) and `POST /v1/sessions` |
| 18 | MEDIUM: SPEC §5.6 forces `COMPLETED_WITH_FAILURES`, contradicting the §3 table; `SUBMITTED`/`OPEN` legs at auth expiry have no end state | adopt | I7 consistency | drop forced status; such legs -> `UNKNOWN`; I2 also covers `SUBMITTED` |
| 19 | MEDIUM: G1/G2/G3 require PASS only (same deadlock as G0); G2 "flag experimental" moot; G4 audits R1–R11 | adopt | avoid gate deadlocks | allow ADJUST-with-actions as in D22; reword G2 adjust row; G4 row -> R1–R12 |
| 20 | MEDIUM: 200-with-error bodies (AngelOne `status:false`, missing order id) not in D23 table | adopt | classification gap | known error codes -> REJECTED; unparseable/missing id -> AMBIGUOUS; one contract case per adapter |
| 21 | MEDIUM: PARTIAL/OPEN legs never re-checked, so the notification goes stale | adopt | accurate result | include in the `recheck_at` sweep |
| 22 | MEDIUM: phase B has ~15 min slack in its window | adopt | budget | D35: C0 instrument master loaded at startup only; adaptive concurrency halving moved to P1 bead B8 |
