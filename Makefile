sources = sqlalchemy_easy_softdelete

.PHONY: lint test coverage clean
lint:
	pre-commit run --all-files

test:
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
	bump2version patch --no-tag
