"""D1 smoke: serve the app, check /ui, then drive the exact API calls the UI makes (Paper).

Exit 0 only if /ui has the broker select + 3 buttons and FIRST_TIME and REBALANCE both reach
COMPLETED. Screenshots (docs/img/) need a local Chrome/Edge; they are skipped with a note if absent.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
IMG = ROOT / "docs" / "img"
KEY = "demo-key"
BROWSERS = (
    "chrome", "google-chrome", "chromium", "msedge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)  # fmt: skip


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def run_flow(c: httpx.Client, mode: str, profile: str | None, payload: dict) -> str:
    creds = {"profile": profile} if profile else {}
    r = c.post("/v1/sessions", json={"broker": "paper", "credentials": creds})
    check(r.status_code == 200, f"{mode}: connect -> session")
    body = {"session_id": r.json()["session_id"], "mode": mode, **payload}
    r = c.post("/v1/executions/preview", json=body)
    legs = r.json()["legs"] if r.status_code == 200 else []
    check(bool(legs), f"{mode}: preview {len(legs)} legs")
    r = c.post("/v1/executions", json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    check(r.status_code == 202, f"{mode}: execute 202")
    run_id = r.json()["run_id"]
    for _ in range(120):
        run = c.get(f"/v1/executions/{run_id}").json()
        if run["status"] in ("COMPLETED", "COMPLETED_WITH_FAILURES", "FAILED"):
            break
        time.sleep(0.5)
    check(run["status"] == "COMPLETED", f"{mode}: run {run['status']} ({len(run['legs'])} legs)")
    notes = run["notifications"]
    check(bool(notes) and notes[-1]["payload"].get("run_id") == run_id, f"{mode}: payload shown")
    check(all(leg["status"] == "FILLED" for leg in run["legs"]), f"{mode}: all legs FILLED")
    return str(run_id)


def screenshot(browser: str, url: str, out: Path) -> None:
    cmd = [browser, "--headless=new", "--disable-gpu", f"--screenshot={out}",
           "--window-size=1000,1300", "--virtual-time-budget=6000", url]  # fmt: skip
    subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    print(("shot " if out.exists() else "skip (browser produced nothing) ") + str(out))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="kalpi-ui-")
    port = free_port()
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{Path(tmp, 'k.db').as_posix()}",
        "FERNET_KEY": Fernet.generate_key().decode(),
        "API_KEYS": f"smoke:{KEY}", "WEBHOOK_SECRET": "smoke-secret", "DEFAULT_WEBHOOK_URL": "",
        "POLL_INTERVAL_S": "0.1", "FRONTEND_DIR": str(ROOT / "frontend"),
    }  # fmt: skip
    cmd = [sys.executable, "-m", "uvicorn", "kalpi_engine.main:app", "--port", str(port)]
    log = open(Path(tmp, "server.log"), "w")  # noqa: SIM115
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, headers={"X-API-Key": KEY}, timeout=15) as c:
            for _ in range(60):
                try:
                    if c.get("/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.5)
            else:
                check(False, "server did not start (see " + str(Path(tmp, "server.log")) + ")")
            r = c.get("/ui")
            html = r.text
            check(r.status_code == 200, "GET /ui 200")
            for needle in ('id="broker"', 'id="connect"', 'id="preview"', 'id="execute"'):
                check(needle in html, f"/ui contains {needle}")
            check(any(b["id"] == "paper" for b in c.get("/v1/brokers").json()), "paper listed")
            # The very payloads the UI textarea is prefilled with (same JSON the page embeds).
            samples = json.loads(html.split('id="samples">')[1].split("</script>")[0])
            first, rebal = samples["FIRST_TIME"], samples["REBALANCE"]
            first_run = run_flow(c, "FIRST_TIME", None, first)
            rebal_run = run_flow(c, "REBALANCE", "demo", rebal)
        browser = next((b for b in map(shutil.which, BROWSERS) if b), None) or next(
            (b for b in BROWSERS if Path(b).is_file()), None
        )
        if browser:
            IMG.mkdir(parents=True, exist_ok=True)
            screenshot(browser, f"{base}/ui?key={KEY}", IMG / "ui-start.png")
            for name, rid in (("first-time", first_run), ("rebalance", rebal_run)):
                screenshot(browser, f"{base}/ui?key={KEY}&run_id={rid}", IMG / f"ui-{name}.png")
        else:
            print("skip screenshots: no Chrome/Edge found")
        print("UI SMOKE PASSED")
        return 0
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
