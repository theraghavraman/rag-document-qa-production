.PHONY: up down test lint migrate logs
up:
	docker compose up --build -d

down:
	docker compose down

test:
	python -m pytest -q

lint:
	ruff check app tests

migrate:
	alembic upgrade head

logs:
	docker compose logs --tail=100 -f api worker beat
