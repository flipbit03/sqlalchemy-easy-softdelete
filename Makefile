sources = sqlalchemy_easy_softdelete

.PHONY: lint typecheck test test-pg test-all-pg coverage clean dev pg pg-stop python-versions

# Run linting (ruff)
lint:
	uv run pre-commit run --all-files

# Run type checking (mypy)
typecheck:
	uv run mypy $(sources)

# Quick test with SQLite (no docker needed)
test:
	uv run pytest

# Test with PostgreSQL (like CI) - requires pg to be running
test-pg:
	TEST_CONNECTION_STRING=postgresql://postgres:postgres@localhost:9991/test_db uv run pytest

# Full test matrix with PostgreSQL (like CI)
# Runs all Python versions × SQLAlchemy versions against PostgreSQL
test-all-pg: pg
	@echo "Running full test matrix against PostgreSQL..."
	TEST_CONNECTION_STRING=postgresql://postgres:postgres@localhost:9991/test_db uv run nox

# Test specific Python version against PostgreSQL
# Usage: make test-py version=3.12
test-py: pg
	TEST_CONNECTION_STRING=postgresql://postgres:postgres@localhost:9991/test_db uv run nox -p $(version)

coverage:
	uv run pytest --cov=$(sources) --cov-branch --cov-report=term-missing --cov-report=xml tests

clean:
	rm -rf .pytest_cache
	rm -rf *.egg-info
	rm -rf .tox dist site .nox
	rm -rf coverage.xml .coverage
	docker compose down -v

dev:
	uv sync --all-groups

# Start PostgreSQL
pg:
	docker compose up -d pg
	@echo "Waiting for PostgreSQL..."
	@until docker compose exec -T pg pg_isready -U postgres 2>/dev/null; do sleep 1; done
	@echo "PostgreSQL is ready!"

# Stop PostgreSQL
pg-stop:
	docker compose down

# Install all Python versions needed for nox
# Run this once to set up multi-version testing
python-versions:
	uv python install 3.10 3.11 3.12 3.13 3.14
