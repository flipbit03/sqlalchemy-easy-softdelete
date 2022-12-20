import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from sqlalchemy_easy_softdelete.handler.rewriter import SoftDeleteQueryRewriter
from tests.model import TestModelBase
from tests.seed_data import generate_table_with_inheritance_obj
from tests.seed_data.parent_child_childchild import generate_parent_child_object_hierarchy

env_connection_string = os.environ.get("TEST_CONNECTION_STRING", None)

test_db_url = env_connection_string or "sqlite://"


@pytest.fixture
def db_engine() -> Engine:
    print(f"connection_string={test_db_url}")
    return create_engine(test_db_url)


@pytest.fixture
def db_connection(db_engine) -> Connection:
    connection = db_engine.connect()

    # start a transaction
    transaction = connection.begin()

    try:
        yield connection
    finally:
        transaction.rollback()
    connection.close()


@pytest.fixture
def db_session(db_connection) -> Session:
    TestModelBase.metadata.create_all(db_connection)
    return sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()


@pytest.fixture
def seeded_session(db_session) -> Session:
    generate_parent_child_object_hierarchy(db_session, 1000)
    generate_parent_child_object_hierarchy(db_session, 1001)
    generate_parent_child_object_hierarchy(db_session, 1002, parent_deleted=True)

    generate_table_with_inheritance_obj(db_session, 1000, deleted=False)
    generate_table_with_inheritance_obj(db_session, 1001, deleted=False)
    generate_table_with_inheritance_obj(db_session, 1002, deleted=True)
    return db_session


@pytest.fixture
def rewriter() -> SoftDeleteQueryRewriter:
    return SoftDeleteQueryRewriter("deleted_at", "include_deleted")
