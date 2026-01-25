import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.default_config.model import SoftDeleteMixin, TestModelBase
from tests.default_config.seed_data import generate_table_with_inheritance_obj
from tests.default_config.seed_data.parent_child_childchild import generate_parent_child_object_hierarchy


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
def rewriter():
    # Access world-specific rewriter from mixin class
    return SoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter
