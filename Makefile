.PHONY: check lint typecheck test itest run up down

check: lint typecheck test

lint:
	uv run ruff check --output-format=concise src tests

typecheck:
	uv run mypy src/kalpi_engine/domain

test:
	uv run pytest -q

itest: up
	uv run pytest -q -m integration tests/integration

run:
	uv run uvicorn kalpi_engine.main:app --reload --port 8000

up:
	docker compose up --build -d

down:
	docker compose down
