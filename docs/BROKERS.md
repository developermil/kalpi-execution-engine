# BROKERS — research ledger (filled by bead A1)

Rule: every row needs a source URL **or** the literal word `UNVERIFIED`. The `broker-researcher` agent writes `docs/brokers/<id>.md` (<=70 lines each); this file holds the one-screen comparison.

## What is already known (from planning-session research; still re-verify in A1)
| Broker | Auth mode (expected) | Notes found so far | Source |
|---|---|---|---|
| Groww | API key+secret (SHA256 checksum of secret+epoch-seconds) **or** TOTP flow; or daily dashboard token (expires 06:00) | Base `https://api.groww.in/v1/`; headers `Authorization: Bearer`, `X-API-VERSION: 1.0`; place order `POST /order/create`; status `GET /order/detail/{groww_order_id}`; has `order_reference_id`; limits orders 10/s, 250/min; static IP not mentioned in docs | https://groww.in/trade-api/docs/curl |
| Zerodha (Kite Connect) | OAuth redirect -> `request_token` -> checksum exchange | Pricing/limits **UNVERIFIED** (pricing page fetch failed) | https://kite.trade/docs/connect/v3/ |
| Upstox | OAuth2 auth-code | Instrument keys are ISIN-based (**UNVERIFIED**) | https://upstox.com/developer/api-documentation |
| Fyers | OAuth auth-code (appIdHash) | Symbol format `NSE:SYMBOL-EQ` (**UNVERIFIED**) | https://myapi.fyers.in/docsv3 |
| AngelOne SmartAPI | client code + PIN + TOTP -> JWT | Needs `symboltoken` from scrip master (**UNVERIFIED**) | https://smartapi.angelone.in/docs |
| SEBI retail algo framework | n/a | static IP, daily 2FA, 10 orders/s, MPP conversion, third-party empanelment (per Fyers notice) | https://fyers.in/notice-board/new-sebi-framework-for-retail-algo-trading-from-april-01-2026/ |

## Comparison table to complete (one row per broker)
| Field | zerodha | upstox | fyers | angelone | groww |
|---|---|---|---|---|---|
| Auth flow + token lifetime | | | | | |
| Holdings endpoint + qty field | | | | | |
| Funds endpoint | | | | | |
| Place-order endpoint + required fields | | | | | |
| Tag / client-order-id field + max length | | | | | |
| Order status / order book endpoint | | | | | |
| Instrument identifier + master file URL | | | | | |
| Rate limits (per s / per min / per day) | | | | | |
| Static IP / registration needed | | | | | |
| Market -> MPP handling | | | | | |
| Free/paid API access | | | | | |
| Error shape + codes for 429 / auth expiry / reject | | | | | |
| Sandbox / test mode available | | | | | |
