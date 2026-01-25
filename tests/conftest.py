import os
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

env_connection_string = os.environ.get("TEST_CONNECTION_STRING", None)


@pytest.fixture
def sqla2_warnings() -> None:
    # Enable SQLAlchemy 2.0 Warnings mode to help with 2.0 support
    os.environ["SQLALCHEMY_WARN_20"] = "1"


@pytest.fixture
def db_engine(sqla2_warnings: None) -> Engine:
    test_db_url = env_connection_string or "sqlite://"
    print(f"connection_string={test_db_url}")
    return create_engine(test_db_url, future=True)


@pytest.fixture
def db_connection(db_engine: Engine) -> Generator[Connection, None, None]:
    connection = db_engine.connect()

    # start a transaction
    transaction = connection.begin()

    try:
        yield connection
    finally:
        transaction.rollback()
    connection.close()
