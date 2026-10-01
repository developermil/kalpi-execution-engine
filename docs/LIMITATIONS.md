# Known limitations (copy into README "Known limitations" in E3)

- OAuth login `state` lives in process memory (D41): if the app restarts during a broker OAuth login, the user must start the login again.
- Single worker only: uvicorn runs with `--workers 1` (D20); OAuth state (D41) and in-flight run tasks are per process. Crash safety comes from the DB lease + resume sweep, not from multiple workers.
- `GET /v1/brokers` is public by design (D40): metadata only, no secrets.
- Fyers order status: the numeric order-book status vocabulary is not in docs/brokers/fyers.md or the fyers-apiv3 SDK 3.1.18 (the SDK only lists an order-history *filter*: 1 Executed, 2 Cancelled, 3 Rejected, a different numbering). The adapter therefore maps no code to FILLED/REJECTED/CANCELLED: legs stay OPEN/PARTIAL (by filled quantity) and end UNKNOWN at poll timeout, never resubmitted (D19). Needs one recorded live response to fix.
- Fyers and Groww: no market-protection / anti-slicing parameter is documented, so none is sent on MARKET orders (`market_order_verified=false`; preview warns MARKET_ORDER_UNVERIFIED).
- Groww auth expiry: only HTTP 401 maps to AuthExpired; the `GA###` expiry codes are unconfirmed, so an expired Groww token may surface as a rejected call instead of a re-login prompt.
- AngelOne: X-ClientLocalIP / X-ClientPublicIP / X-MACAddress come from ANGELONE_CLIENT_LOCAL_IP / ANGELONE_CLIENT_PUBLIC_IP / ANGELONE_MAC; login fails with a clear error if unset. DELIVERY product code, order-book field names and status vocabulary are UNVERIFIED.
- All five real broker adapters are `experimental=true`, `live_tested=false`: contract-tested against mocked HTTP only.
