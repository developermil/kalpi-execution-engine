# --- build stage: resolve deps into a venv with uv ---
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# --- runtime stage: slim, non-root ---
FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 10001 app
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app frontend ./frontend
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
# --workers 1 is load-bearing: run leases assume one executor process (D20)
CMD ["uvicorn", "kalpi_engine.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
