#!/usr/bin/env bash
# README "Quick start" + "Try it" commands, verbatim, against the Paper broker on the compose stack.
# Exit 0 only if the stack is healthy, a REBALANCE run COMPLETES, and the webhook was received.
# Leaves the stack running (never runs `down -v`: it would delete your Postgres volume).
set -euo pipefail
cd "$(dirname "$0")/.."

# --- README: Quick start ---------------------------------------------------------------------
[ -f .env ] || cp .env.example .env
KEY=$(python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
grep -q '^FERNET_KEY=.\+' .env || sed -i "s|^FERNET_KEY=.*|FERNET_KEY=$KEY|" .env
docker compose up --build -d
# --- end Quick start (the wait loop below is only so this script can proceed) ----------------
for _ in $(seq 1 60); do curl -sf localhost:8000/healthz >/dev/null && break; sleep 2; done
curl -s localhost:8000/healthz

# --- README: Try it with curl ----------------------------------------------------------------
API=localhost:8000
APIKEY=$(grep -m1 '^API_KEYS=' .env | cut -d= -f2- | cut -d, -f1 | cut -d: -f2)
H=(-H "X-API-Key: $APIKEY" -H "Content-Type: application/json")
SID=$(curl -s "${H[@]}" -X POST $API/v1/sessions \
  -d '{"broker":"paper","credentials":{"profile":"demo"}}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
BODY='{"session_id":"'$SID'","mode":"REBALANCE","instructions":[
  {"symbol":"TCS","action":"SELL","quantity":2},
  {"symbol":"INFY","action":"REBALANCE","side":"BUY","quantity":3},
  {"symbol":"HDFCBANK","action":"BUY","quantity":4}]}'
curl -s "${H[@]}" -X POST $API/v1/executions/preview -d "$BODY"; echo
RUN=$(curl -s "${H[@]}" -H "Idempotency-Key: demo-$(date +%s)" -X POST $API/v1/executions -d "$BODY" \
  | python -c "import sys,json; print(json.load(sys.stdin)['run_id'])")
sleep 3
curl -s "${H[@]}" $API/v1/executions/$RUN | python -m json.tool | head -40
# --- end Try it ------------------------------------------------------------------------------

STATUS=$(curl -s "${H[@]}" $API/v1/executions/$RUN | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
HOOK=$(curl -s $API/mock/webhook | python -c "import sys,json; print(sum(1 for p in json.load(sys.stdin) if p.get('run_id')=='$RUN'))")
echo "run $RUN status=$STATUS webhooks_received=$HOOK"
[ "$STATUS" = "COMPLETED" ] && [ "$HOOK" -ge 1 ]
