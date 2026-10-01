# PLAN v0 — Portfolio Trade Execution Engine (status: DRAFT until adversarial review + audit + commit)

Task graph lives in `docs/beads.toml` (use `python scripts/beads.py ready|show|close`). This file holds the parts that are not tasks: gates, failure scenarios, insight tiers, invariants, schedule. Decisions: `docs/DECISIONS.md`. Contracts: `docs/SPEC.md`.

## Numbers
- 33 beads = 25 tasks + 5 gates + 3 stretch. Non-stretch effort ≈ 1260 min (gates included); P0 ≈ 1130 min.
- **Critical path 690 min (11.5 h)** if independent beads run in parallel: A2→A3→A4→A5→G0→B2→B4→B6→B7→G1→C0→C1→C6→G2→E3→E4→E5→G4.
- Sequential execution would be ~21 h: **parallelism is mandatory** (A1 research ‖ A2–A5; B1 ‖ B2 ‖ B3; C1–C5 five-way; D1 ‖ C-phase; E1 ‖ C-phase).

## Schedule (H = hours from start; target ship at H16, H16–24 is buffer)
| Window | Work | Exit |
|---|---|---|
| H0:00–1:30 | Review kit: `plan-critic`, `/audit` of PROMPTS, edit, commit. **Start A1 research in background at H0:15.** | Plan frozen + committed |
| H1:30–4:30 | A2→A5 (A1 finishing in parallel) | **G0** |
| H4:30–8:00 | B1‖B2‖B3 → B5 → B4 (opus design, then build) → B6 → B7 | **G1** |
| H8:00–11:30 | C0 → C1…C5 in parallel (worktrees/subagents) ‖ D1 ‖ E1 → C6 | **G2, G3** |
| H11:30–15:30 | E2 → E3 README → E4 clean-room → E5 audit | **G4** (push public repo) |
| H15:30–24:00 | Buffer: live smoke on one real broker, fix WEAK items, S1 if time. **Do not start new scope after H20.** | Submit |

## Gates (Pass / Adjust / Abort — measured values only; "looks good" is not a gate)
**G0 Feasibility** (after A1+A5)
| Check | Pass | Adjust | Abort/escalate |
|---|---|---|---|
| A5 spike prints 5 FILLED legs via the real port | yes, <5 s | works but port needed changes -> update SPEC §4 then continue | cannot express Paper through the port -> redesign port before anything else |
| BROKERS.md UNVERIFIED cells per broker | <=3 each | 4–6 for one broker -> apply F1 cascade to that broker | >=3 brokers with >6 -> F1 step 4 (flag experimental, tell human) |
| Elapsed since start | <=H4:30 | H4:30–6:00 -> cut D1 to minimal (F12) | >H6:00 -> drop S*, drop E1 detail, tell human |

**G1 Engine correct on Paper** (after B7)
| Check | Pass | Adjust | Abort |
|---|---|---|---|
| Invariants I1–I7 over 20 seeds | 20/20 | 18–19 -> fix + rerun; max 2 loops | <18 after 2 loops -> stop, re-read SPEC §5, escalate |
| Planner coverage | >=95% | 85–94% -> add cases | <85% |
| Duplicate orders at Paper | 0 | — | any >0 -> **hard stop**, fix before any adapter work |

**G2 Adapters** (after C6)
| Check | Pass | Adjust | Abort |
|---|---|---|---|
| Contract suite green per adapter | 5/5 | 4/5 -> remaining flagged `experimental` (F1.3) | <=3/5 -> F1.4 and tell human immediately |
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
| Audit R1–R11 | 0 MISSING | WEAK only -> list in README Limitations | any MISSING -> fix first |
| Public repo clones & runs from README alone | yes | typo fixes | no |

## Failure scenarios (numbered cascades: try cheapest first)
| ID | Failure | Detect | Recovery cascade |
|---|---|---|---|
| F1 | Broker API can't be verified or adapter infeasible | A1 UNVERIFIED count; contract test can't be written | 1) re-run researcher using official docs + reading the official SDK source as reference · 2) wrap official SDK with `asyncio.to_thread` for that broker only · 3) ship adapter `experimental=true` with contract tests + README disclosure · 4) fewer documented adapters + Paper, tell the human; **never claim live-tested** |
| F2 | 429 / rate-limit storm | `RateLimited` events | 1) honour `Retry-After` · 2) jittered backoff · 3) halve concurrency for the session · 4) after 5 attempts mark leg `FAILED(RATE_LIMITED)`, continue others |
| F3 | Ambiguous submit (risk of duplicate order) | timeout/5xx after send | 1) `find_order_by_tag` ×3 with backoff · 2) if broker lacks tag: match order book on symbol+side+qty+time window, mark `matched_heuristically` · 3) `UNKNOWN`, **no resubmit**, surface in notification |
| F4 | Session expired mid-run | `AuthExpired` | 1) stop placing · 2) remaining legs `SKIPPED(AUTH_EXPIRED)` · 3) notify; user reconnects and starts a new run with a new key (resume endpoint is stretch) |
| F5 | Insufficient funds / RMS reject | broker reject text | 1) leg `REJECTED(reason)` · 2) continue others · 3) preview warns if `get_funds` supported |
| F6 | Order still OPEN at poll timeout | poll timeout | leave `OPEN`, report, never auto-cancel |
| F7 | Process crash mid-run | startup scan of non-terminal runs | 1) reconcile `SUBMITTING` legs by tag · 2) continue only `PLANNED` legs · 3) `UNKNOWN` for unresolvable |
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
I1 at most one broker order per leg tag · I2 no leg left in `PLANNED`/`SUBMITTING` at run end · I3 no buy leg submitted before all sell legs are terminal (unless halted) · I4 notification lists every non-filled leg; counts add up to total · I5 no secret in logs/events · I6 same Idempotency-Key → same run, no extra orders · I7 run status is a pure function of leg statuses.

## Execution strategy (token efficiency + compaction survival)
1. State lives in files: `docs/state.json` (bead status), `docs/progress.md` (auto-appended on close), `docs/HANDOFF.md` (<=15 lines, via `/handoff`). Conversation is disposable.
2. Never read whole docs: `beads.py show <id>` gives the bead plus the exact SPEC section to read.
3. Heavy reading/output goes to subagents returning <=200 words (broker research, log triage, test failure triage).
4. Model split: opus for plan critique, B4 design and gate verdicts; sonnet for building; haiku acceptable for log summarising.
5. Commit per bead (`feat(B4): ...`). Green commit hash goes into the bead evidence.
6. `/clear` at each gate. `/resume` restores context in ~2k tokens.
7. Test output: `pytest -q -x --tb=short`, `ruff check --output-format=concise`; never paste full logs back into the chat.

## Adversarial review log (fill after running the `plan-critic` agent)
| # | Critique | Verdict (adopt / reject) | Reason | Plan change |
|---|---|---|---|---|
| | | | | |
