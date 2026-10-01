# Known limitations (copy into README "Known limitations" in E3)

- OAuth login `state` lives in process memory (D41): if the app restarts during a broker OAuth login, the user must start the login again.
- Single worker only: uvicorn runs with `--workers 1` (D20); OAuth state (D41) and in-flight run tasks are per process. Crash safety comes from the DB lease + resume sweep, not from multiple workers.
- `GET /v1/brokers` is public by design (D40): metadata only, no secrets.
