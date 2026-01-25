import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.custom_deleted_field_name.model import CFNModelBase, CFNSoftDeleteMixin


@pytest.fixture
def db_session(db_connection) -> Session:
    CFNModelBase.metadata.create_all(db_connection)
    return sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()


@pytest.fixture
def rewriter():
    return CFNSoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter
