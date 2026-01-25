sources = sqlalchemy_easy_softdelete

.PHONY: lint test docker_test coverage clean dev pg

lint:
	pre-commit run --all-files

test:
	uv run pytest

nox:
	uv run nox

docker_test: clean
	docker compose build tests-with-coverage
	docker compose --env-file .env.docker run tests

coverage:
	uv run pytest --cov=$(sources) --cov-branch --cov-report=term-missing --cov-report=xml tests

clean:
	rm -rf .pytest_cache
	rm -rf *.egg-info
	rm -rf .tox dist site
	rm -rf coverage.xml .coverage
	docker compose down -v

dev:
	uv sync --all-groups

pg:
	# Start Postgres Instance
	docker compose up -d pg
