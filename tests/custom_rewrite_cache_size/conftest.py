from typing import cast

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from sqlalchemy_easy_softdelete.handler.rewriter import SoftDeleteQueryRewriter
from tests.custom_rewrite_cache_size.model import CRCModelBase, CRCSoftDeleteMixin


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    CRCModelBase.metadata.create_all(db_connection)  # type: ignore[attr-defined]
    return sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()


@pytest.fixture
def rewriter() -> SoftDeleteQueryRewriter:
    return cast(SoftDeleteQueryRewriter, CRCSoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter)
