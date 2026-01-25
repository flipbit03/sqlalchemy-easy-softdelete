from typing import cast

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from sqlalchemy_easy_softdelete.handler.rewriter import SoftDeleteQueryRewriter
from tests.default_config.model import SoftDeleteMixin, TestModelBase
from tests.default_config.seed_data import generate_table_with_inheritance_obj
from tests.default_config.seed_data.parent_child_childchild import generate_parent_child_object_hierarchy


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    TestModelBase.metadata.create_all(db_connection)  # type: ignore[attr-defined]
    return sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()


@pytest.fixture
def seeded_session(db_session: Session) -> Session:
    generate_parent_child_object_hierarchy(db_session, 1000)
    generate_parent_child_object_hierarchy(db_session, 1001)
    generate_parent_child_object_hierarchy(db_session, 1002, parent_deleted=True)

    generate_table_with_inheritance_obj(db_session, 1000, deleted=False)
    generate_table_with_inheritance_obj(db_session, 1001, deleted=False)
    generate_table_with_inheritance_obj(db_session, 1002, deleted=True)
    return db_session


@pytest.fixture
def rewriter() -> SoftDeleteQueryRewriter:
    return cast(SoftDeleteQueryRewriter, SoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter)
