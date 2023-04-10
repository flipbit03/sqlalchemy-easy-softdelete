sources = sqlalchemy_easy_softdelete

.PHONY: lint test coverage clean
lint:
	pre-commit run --all-files

test:
	pytest

test_pg:
	export TEST_CONNECTION_STRING=postgresql://postgres:postgres@127.0.0.1:9991/test_db
	pytest

coverage:
	pytest --cov=$(sources) --cov-branch --cov-report=term-missing --cov-report=xml tests

clean:
	rm -rf .pytest_cache
	rm -rf *.egg-info
	rm -rf .tox dist site
	rm -rf coverage.xml .coverage

dev:
	# Start Postgres Instance
	docker compose up -d pg

bump_patch:
	bump2version patch --no-tag --no-commit
