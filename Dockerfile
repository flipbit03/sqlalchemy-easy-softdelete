FROM python:3.10.1 as base

ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONFAULTHANDLER 1

RUN pip install --upgrade pip && \
    pip install poetry

FROM base as content

WORKDIR /library

# Copy project files
COPY pyproject.toml .
COPY poetry.lock .
COPY setup.cfg .
COPY README.md .
COPY sqlalchemy_easy_softdelete sqlalchemy_easy_softdelete
COPY tests tests

RUN poetry install

FROM content as testing_and_coverage

CMD sleep 2 && poetry run pytest --cov=sqlalchemy_easy_softdelete --cov-branch --cov-report=term-missing --cov-report=xml tests
