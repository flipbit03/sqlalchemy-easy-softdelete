import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.integer_field_type.model import IFTModelBase, IFTSoftDeleteMixin


@pytest.fixture
def db_session(db_connection) -> Session:
    IFTModelBase.metadata.create_all(db_connection)
    return sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()


@pytest.fixture
def rewriter():
    return IFTSoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter
