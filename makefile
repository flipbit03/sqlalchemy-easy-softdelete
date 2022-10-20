sources = sqlalchemy_easy_softdelete

.PHONY: test format lint unittest coverage pre-commit clean
test: lint unittest

lint:
	flake8 $(sources) tests

unittest:
	pytest

coverage:
	pytest --cov=$(sources) --cov-branch --cov-report=term-missing --cov-report=xml tests

pre-commit:
	pre-commit run --all-files

clean:
	rm -rf .pytest_cache
	rm -rf *.egg-info
	rm -rf .tox dist site
	rm -rf coverage.xml .coverage
