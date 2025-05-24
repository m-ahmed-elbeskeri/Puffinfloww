.PHONY: help install dev test lint format clean docker-up docker-down

help:
	@echo "Available commands:"
	@echo "  install      Install dependencies"
	@echo "  dev          Run development server"
	@echo "  test         Run tests"
	@echo "  lint         Run linters"
	@echo "  format       Format code"
	@echo "  clean        Clean cache files"
	@echo "  docker-up    Start services with docker-compose"
	@echo "  docker-down  Stop docker services"

install:
	poetry install

install-enterprise:
	poetry install --with enterprise

dev:
	poetry run uvicorn core.api.rest.app:app --reload --host 0.0.0.0 --port 8000

worker:
	poetry run workflow-worker --queue default

test:
	poetry run pytest tests/ -v --cov=core --cov-report=html

test-integration:
	poetry run pytest tests/integration/ -v

lint:
	poetry run ruff check .
	poetry run mypy core/

format:
	poetry run black .
	poetry run ruff check --fix .

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .coverage htmlcov/ .pytest_cache/ .mypy_cache/ .ruff_cache/

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f

migrate:
	poetry run alembic upgrade head

migrate-create:
	poetry run alembic revision --autogenerate -m "$(message)"