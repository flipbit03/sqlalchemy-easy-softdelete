from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from tests.sibling_loader_paths.model import SLChild, SLModelBase, SLParent


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    SLModelBase.metadata.create_all(db_connection)
    session = sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()
    session.add(SLParent(id=1))
    session.flush()
    # Child 2 is soft-deleted.
    session.add_all([SLChild(id=1, parent_id=1), SLChild(id=2, parent_id=1, deleted_at=datetime.now(timezone.utc))])
    session.flush()
    session.expunge_all()
    return session
