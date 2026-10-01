#!/usr/bin/env python3
"""Tiny dependency-aware task graph (stdlib only, Python 3.11+). Saves tokens: Claude never loads the whole plan.

  python scripts/beads.py lint                 validate the graph (refs, cycles, required fields)
  python scripts/beads.py ready                list unblocked beads (compact)
  python scripts/beads.py show <id>            one bead + dependency status
  python scripts/beads.py start <id>           mark in_progress (refuses if blocked)
  python scripts/beads.py close <id> -e "..."  mark done with evidence; appends to docs/progress.md
  python scripts/beads.py reopen <id>
  python scripts/beads.py status               progress summary
Env overrides: BEADS_FILE, BEADS_STATE, PROGRESS_FILE
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BEADS_FILE = Path(os.environ.get("BEADS_FILE", ROOT / "docs" / "beads.toml"))
STATE_FILE = Path(os.environ.get("BEADS_STATE", ROOT / "docs" / "state.json"))
PROGRESS_FILE = Path(os.environ.get("PROGRESS_FILE", ROOT / "docs" / "progress.md"))
REQUIRED = ["id", "phase", "kind", "priority", "blocked_by", "est", "title", "what", "done_when", "if_fails"]
PRIORITIES = {"P0": 0, "P1": 1, "P2": 2}


def load():
    with open(BEADS_FILE, "rb") as f:
        beads = tomllib.load(f).get("bead", [])
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    return beads, {b["id"]: b for b in beads}, state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def status_of(state, bid):
    return state.get(bid, {}).get("status", "open")


def blockers(by_id, state, bead):
    return [d for d in bead["blocked_by"] if status_of(state, d) != "done"]


def lint(beads, by_id):
    errs = []
    seen = set()
    for b in beads:
        bid = b.get("id", "?")
        if bid in seen:
            errs.append(f"{bid}: duplicate id")
        seen.add(bid)
        for k in REQUIRED:
            if k not in b or b[k] in ("", None):
                if k == "blocked_by" and b.get(k) == []:
                    continue
                errs.append(f"{bid}: missing/empty field '{k}'")
        if b.get("priority") not in PRIORITIES:
            errs.append(f"{bid}: priority must be P0/P1/P2")
        if b.get("kind") not in ("task", "gate"):
            errs.append(f"{bid}: kind must be task|gate")
        if not isinstance(b.get("est", 0), int) or b.get("est", 0) <= 0:
            errs.append(f"{bid}: est must be positive int minutes")
        elif b["est"] > 90 and b.get("kind") == "task":
            errs.append(f"{bid}: est {b['est']}m > 90m, split this fat bead")
        for d in b.get("blocked_by", []):
            if d not in by_id:
                errs.append(f"{bid}: blocked_by unknown id '{d}'")
        if b.get("kind") == "gate" and not b.get("blocked_by"):
            errs.append(f"{bid}: gate must have blockers")
    # cycle detection (Kahn)
    indeg = {b["id"]: len([d for d in b.get("blocked_by", []) if d in by_id]) for b in beads if "id" in b}
    queue = [i for i, n in indeg.items() if n == 0]
    visited = 0
    while queue:
        n = queue.pop()
        visited += 1
        for b in beads:
            if n in b.get("blocked_by", []):
                indeg[b["id"]] -= 1
                if indeg[b["id"]] == 0:
                    queue.append(b["id"])
    if visited != len(indeg):
        stuck = sorted(i for i, n in indeg.items() if n > 0)
        errs.append(f"cycle detected among: {', '.join(stuck)}")
    return errs


def cmd_lint(beads, by_id, state, a):
    errs = lint(beads, by_id)
    for e in errs:
        print("ERROR", e)
    if errs:
        return 1
    total = sum(b["est"] for b in beads)
    p0 = sum(b["est"] for b in beads if b["priority"] == "P0")
    print(f"OK: {len(beads)} beads, {total} min total ({p0} min P0), no cycles")
    return 0


def cmd_ready(beads, by_id, state, a):
    rows = [b for b in beads if status_of(state, b["id"]) == "open" and not blockers(by_id, state, b)]
    rows.sort(key=lambda b: PRIORITIES[b["priority"]])
    ip = [b["id"] for b in beads if status_of(state, b["id"]) == "in_progress"]
    if ip:
        print("IN PROGRESS:", ", ".join(ip))
    if not rows:
        print("nothing ready" if not ip else "")
        return 0
    for b in rows:
        tag = "GATE" if b["kind"] == "gate" else b["phase"]
        print(f"{b['id']:<4} [{b['priority']}] {tag:<4} {b['est']:>3}m  {b['title']}")
    return 0


def cmd_show(beads, by_id, state, a):
    b = by_id.get(a.id)
    if not b:
        print(f"unknown bead {a.id}")
        return 1
    print(f"{b['id']} · {b['title']}  [{b['priority']}, {b['kind']}, {b['est']}m, status={status_of(state, b['id'])}]")
    print(f"model: {b.get('model', '-')}")
    print(f"WHAT: {b['what']}")
    print(f"DONE WHEN: {b['done_when']}")
    print(f"IF IT FAILS: {b['if_fails']} (see docs/PLAN.md §Failure scenarios)")
    print(f"READ ONLY: {b.get('spec', '-')}   REQS: {', '.join(b.get('reqs', [])) or '-'}")
    if b["blocked_by"]:
        print("BLOCKED BY: " + ", ".join(f"{d}={status_of(state, d)}" for d in b["blocked_by"]))
    return 0


def cmd_start(beads, by_id, state, a):
    b = by_id.get(a.id)
    if not b:
        print(f"unknown bead {a.id}")
        return 1
    bl = blockers(by_id, state, b)
    if bl and not a.force:
        print(f"BLOCKED by: {', '.join(bl)}")
        return 1
    state[a.id] = {**state.get(a.id, {}), "status": "in_progress", "started": datetime.now().astimezone().isoformat(timespec="minutes")}
    save_state(state)
    print(f"{a.id} in_progress")
    return 0


def cmd_close(beads, by_id, state, a):
    b = by_id.get(a.id)
    if not b:
        print(f"unknown bead {a.id}")
        return 1
    if not a.evidence.strip():
        print("evidence is required (measured result against done_when)")
        return 1
    bl = blockers(by_id, state, b)
    if bl and not a.force:
        print(f"BLOCKED by: {', '.join(bl)}")
        return 1
    now = datetime.now().astimezone().isoformat(timespec="minutes")
    state[a.id] = {**state.get(a.id, {}), "status": "done", "evidence": a.evidence.strip(), "closed": now}
    save_state(state)
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(f"- {now.replace('T', ' ')} | {a.id} done | {a.evidence.strip()}\n")
    print(f"{a.id} done; logged to {PROGRESS_FILE.name}")
    return 0


def cmd_reopen(beads, by_id, state, a):
    state.pop(a.id, None)
    save_state(state)
    print(f"{a.id} reopened")
    return 0


def cmd_status(beads, by_id, state, a):
    phases = {}
    for b in beads:
        p = phases.setdefault(b["phase"], [0, 0, 0])
        p[1] += 1
        p[2] += b["est"]
        if status_of(state, b["id"]) == "done":
            p[0] += 1
    done_min = sum(b["est"] for b in beads if status_of(state, b["id"]) == "done")
    total_min = sum(b["est"] for b in beads)
    for k in sorted(phases):
        d, t, _ = phases[k]
        print(f"phase {k}: {d}/{t} done")
    print(f"effort: {done_min}/{total_min} planned minutes done")
    ip = [b["id"] for b in beads if status_of(state, b["id"]) == "in_progress"]
    print("in progress:", ", ".join(ip) if ip else "-")
    return 0


def main():
    p = argparse.ArgumentParser(prog="beads")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lint")
    sub.add_parser("ready")
    sub.add_parser("status")
    for name in ("show", "start", "close", "reopen"):
        sp = sub.add_parser(name)
        sp.add_argument("id")
        if name in ("start", "close"):
            sp.add_argument("--force", action="store_true")
        if name == "close":
            sp.add_argument("-e", "--evidence", default="")
    a = p.parse_args()
    beads, by_id, state = load()
    fn = {"lint": cmd_lint, "ready": cmd_ready, "show": cmd_show, "start": cmd_start,
          "close": cmd_close, "reopen": cmd_reopen, "status": cmd_status}[a.cmd]
    sys.exit(fn(beads, by_id, state, a))


if __name__ == "__main__":
    main()
