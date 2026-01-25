import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.disabled_methods.model import DMModelBase, DMSoftDeleteMixin


@pytest.fixture
def db_session(db_connection) -> Session:
    DMModelBase.metadata.create_all(db_connection)
    return sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()


@pytest.fixture
def rewriter():
    return DMSoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter
