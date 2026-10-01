.PHONY: check lint typecheck test itest run up down

check: lint typecheck test

lint:
	uv run ruff check --output-format=concise src tests

typecheck:
	uv run mypy src/kalpi_engine/domain

test:
	uv run pytest -q

ITEST_COMPOSE = docker compose -p kalpi-itest -f docker-compose.yml -f docker-compose.itest.yml

# Fresh stack + empty Postgres volume every time; always torn down, exit code is pytest's.
itest:
	$(ITEST_COMPOSE) down -v --remove-orphans
	$(ITEST_COMPOSE) up --build -d --wait
	uv run pytest -q -m integration tests/integration; rc=$$?; 	  $(ITEST_COMPOSE) logs app --tail 40 > .itest-app.log 2>&1; 	  $(ITEST_COMPOSE) down -v --remove-orphans; exit $$rc

run:
	uv run uvicorn kalpi_engine.main:app --reload --port 8000

up:
	docker compose up --build -d

down:
	docker compose down
