import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.custom_default_value.model import CDVModelBase, CDVSoftDeleteMixin


@pytest.fixture
def db_session(db_connection) -> Session:
    CDVModelBase.metadata.create_all(db_connection)
    return sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()


@pytest.fixture
def rewriter():
    return CDVSoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter
