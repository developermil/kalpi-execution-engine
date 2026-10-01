# Adding a broker (one file + one fixture, zero core edits)

Proof: `tests/contract/_dummy/brokers/dummy.py` is a complete HTTP broker. The contract suite and
`GET /v1/brokers` pick it up with no edit to any shared file (`test_dummy_*` in
`tests/contract/test_contract.py`). Worked examples: `src/kalpi_engine/brokers/zerodha.py` (OAuth),
`angelone.py` (credentials + TOTP).

## Checklist

1. **Research first**: write `docs/brokers/<id>.md` from the broker's primary docs. Mark every
   fact you could not confirm `UNVERIFIED` (D19).
2. **Adapter**: create `src/kalpi_engine/brokers/<id>.py` with one class extending
   `HttpBrokerAdapter` (`brokers/base.py`). It is auto-discovered by `registry.discover()`; do not
   register it anywhere. Never import another broker. No broker SDK, no `to_thread` (D25).
3. **`meta`**: `id`, `auth_mode`, `credential_fields` (non-OAuth), `rate_limits` (the LOWEST of any
   conflicting documented limits), `tag_max_len` (the shorter documented limit),
   `requires_static_ip`, `daily_2fa`. Ship `experimental=True`, `live_tested=False`,
   `market_order_verified=False` until a real call succeeded.
4. **Implement** `create_session`, `get_holdings`, `get_funds`, `resolve_instrument`,
   `place_order`, `get_order`, `find_order_by_tag` (+ `login_url` for OAuth).
   - Build an `InstrumentResolver(fetch, parse)` in `__init__`; optionally warm it in `startup()`.
   - `holdings.sellable_qty` = the most conservative field (minus T+1, used, pledged).
   - MARKET orders send the broker's market-protection / anti-slicing parameter, if one exists.
   - Map unknown order statuses to non-terminal OPEN. Never FILLED, never resubmit (D9).
   - Override `error_code`, `auth_error_codes`, `order_id_from` for the broker's error envelope.
   - Credentials come from constructor args with env defaults; never log or persist secrets.
   - Source-comment every endpoint; keep the file <= 200 lines.
5. **Fixture**: create `tests/contract/fixtures/<id>.py` implementing the hooks listed in the
   `tests/contract/harness.py` docstring (`make_adapter`, `login`, `holdings`, `place_ok`,
   `rate_limited`, `auth_expired`, `reject`, `error_200`, `timeout`, `tag_hit`, `tag_miss`,
   `resolve`, plus `checksum_vectors` if requests are signed). `place_ok.expect` MUST assert the
   broker-specific payload fields (e.g. `market_protection`).
6. **Verify**: `uv run pytest -q tests/contract -k <id>` (12 cases), then `make check`.
7. **Disclose**: add anything unconfirmed to `docs/LIMITATIONS.md`.

Files you may touch: `brokers/<id>.py`, `fixtures/<id>.py`, `docs/brokers/<id>.md`,
`.env.example` (credentials), `docs/LIMITATIONS.md`. Nothing else.
