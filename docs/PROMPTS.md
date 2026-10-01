# PROMPTS — verbatim requirement log (audit this before freezing the plan)

Rule: append every instruction from the human here, with the decisions it implies. At plan freeze, `/audit` checks each prompt against the beads.

### Prompt 1 — Mission & workflow
> Starting a new backend project for a systematic quant investing platform based out of India. Set up a workflow for Claude Code to get through it within 24 hours, most efficient in tokens and time, giving a quality product. Plan -> Build (all phases planned even for further projects). Follow the "How to Build a Perfect Plan" workflow.

**Decisions:** plan-first; token efficiency is a hard constraint; plan covers all phases; beads graph + gates + failure scenarios + adversarial review + progress-in-files.

### Prompt 2 — Task + scope
> This is our task at hand (Kalpi Builder Assignment v2). Analyse patiently, tradeoffs etc. Go with Problem Statement 1 first. Get the starter kit done, then give steps to proceed.

**Decisions:** Problem 1 only (Portfolio Trade Execution Engine); Problem 2 (data platform design doc) is a later, separate session; starter kit = Claude Code config + plan v0; decisions with explicit trade-offs.

### Prompt 3 — (append here)
