# ruff: noqa: F811  (pytest fixtures imported from test_api and used by name)
"""File upload in the UI: the real browser parser (frontend/portfolio.js) run under node."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.unit.test_api import H, client, paper_session, reg, settings  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "docs" / "samples"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")
DRIVER = (
    "const P=require(process.argv[1]);const [n,t]=JSON.parse(require('fs').readFileSync(0,'utf8'));"
    "try{console.log(JSON.stringify({ok:P.parsePortfolioFile(n,t)}))}"
    "catch(e){console.log(JSON.stringify({err:e.message}))}"
)


def parse(name: str, text: str) -> dict[str, Any]:
    assert NODE
    out = subprocess.run(
        [NODE, "-e", DRIVER, str(ROOT / "frontend" / "portfolio.js")],
        input=json.dumps([name, text]), capture_output=True, text=True, check=True, timeout=30,
    )  # fmt: skip
    result: dict[str, Any] = json.loads(out.stdout)
    return result


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


@needs_node
def test_sample_json_and_csv_parse_to_the_same_instructions() -> None:
    j = parse("rebalance.json", sample("rebalance.json"))["ok"]
    c = parse("rebalance.csv", sample("rebalance.csv"))["ok"]
    assert j["mode"] == "REBALANCE" and len(j["instructions"]) == 3
    assert c["instructions"] == j["instructions"]
    assert c["options"]["order_type"] == "MARKET"


@needs_node
@pytest.mark.parametrize(
    ("name", "text", "expect"),
    [
        ("p.json", "{not json", "Invalid JSON"),
        ("p.json", '{"options": {}}', '"instructions" array'),
        ("p.json", '{"instructions": []}', "no instructions"),
        ("p.csv", "", "empty"),
        ("p.csv", "symbol,action\nTCS,BUY", "missing: quantity"),
        ("p.csv", "symbol,action,quantity,colour\nTCS,BUY,1,red", "Unknown CSV column"),
        ("p.csv", "symbol,action,quantity\nTCS,HOLD,1", 'Line 2: action "HOLD"'),
        ("p.csv", "symbol,action,quantity\nTCS,BUY,1.5", "Line 2: quantity"),
        ("p.csv", "symbol,action,quantity\nTCS,BUY,1\nINFY,SELL,0", "Line 3: quantity"),
        ("p.csv", "symbol,action,quantity,side\nTCS,REBALANCE,2,", "REBALANCE needs a side"),
        ("p.csv", "symbol,action,quantity,order_type\nTCS,BUY,2,LIMIT", "needs limit_price"),
        ("p.csv", "symbol,action,quantity\nTCS,BUY", "expected 3 columns"),
        ("p.csv", "symbol,action,quantity", "no rows"),
        ("p.txt", "TCS,BUY,1", "choose a .json or .csv"),
    ],
)
def test_malformed_files_give_a_clear_error(name: str, text: str, expect: str) -> None:
    assert expect in parse(name, text)["err"]


@needs_node
def test_csv_edge_cases_bom_quotes_blank_lines_case() -> None:
    text = '﻿Symbol,Action,Quantity\r\n\r\n"tcs", buy ,"3"\r\n'
    got = parse("x.CSV", text)["ok"]["instructions"]
    assert got == [{"symbol": "TCS", "action": "BUY", "quantity": 3}]


@needs_node
@pytest.mark.parametrize("fname", ["rebalance.json", "rebalance.csv"])
def test_parsed_sample_previews_on_paper(client: TestClient, fname: str) -> None:
    parsed = parse(fname, sample(fname))["ok"]
    sid = paper_session(client, {"TCS": 5, "INFY": 8})
    payload = {k: v for k, v in parsed.items() if k != "mode"}
    body = {"session_id": sid, "mode": "REBALANCE", **payload}
    r = client.post("/v1/executions/preview", json=body, headers=H)
    assert r.status_code == 200, r.text
    assert [(leg["phase"], leg["symbol"]) for leg in r.json()["legs"]] == [
        ("SELL", "TCS"), ("BUY", "INFY"), ("BUY", "HDFCBANK"),
    ]  # fmt: skip


def test_ui_serves_file_input_and_parser(client: TestClient) -> None:
    page = client.get("/ui")
    assert 'id="file"' in page.text and ".json" in page.text and ".csv" in page.text
    for needle in ('id="fileinfo"', "Loaded: ${f.name}", "Failed: ${f.name}", "Edited manually"):
        assert needle in page.text
    js = client.get("/ui/portfolio.js")
    assert js.status_code == 200 and "text/javascript" in js.headers["content-type"]
    assert "parsePortfolioFile" in js.text
