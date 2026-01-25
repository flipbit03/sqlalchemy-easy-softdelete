import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.custom_method_names.model import CMNModelBase, CMNSoftDeleteMixin


@pytest.fixture
def db_session(db_connection) -> Session:
    CMNModelBase.metadata.create_all(db_connection)
    return sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()


@pytest.fixture
def rewriter():
    return CMNSoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter
