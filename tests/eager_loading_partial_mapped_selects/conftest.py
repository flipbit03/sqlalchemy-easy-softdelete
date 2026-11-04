from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from tests.eager_loading_partial_mapped_selects.model import PMChild, PMModelBase, PMParent


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    PMModelBase.metadata.create_all(db_connection)
    session = sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()
    deleted = datetime.now(timezone.utc)
    # Parent 3 is soft-deleted.
    session.add_all([PMParent(id=1), PMParent(id=2), PMParent(id=3, deleted_at=deleted)])
    session.flush()
    # Child 2 is soft-deleted. Child 4 is active, under the soft-deleted parent 3.
    session.add_all([
        PMChild(id=1, parent_id=1),
        PMChild(id=2, parent_id=1, deleted_at=deleted),
        PMChild(id=3, parent_id=2),
        PMChild(id=4, parent_id=3),
    ])
    session.flush()
    session.expunge_all()
    return session
