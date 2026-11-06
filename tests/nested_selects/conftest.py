from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from tests.nested_selects.model import NSChild, NSModelBase, NSOwner, NSParent


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    NSModelBase.metadata.create_all(db_connection)
    session = sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()
    deleted = datetime.now(timezone.utc)
    # Parent 4 is soft-deleted.
    session.add_all([NSParent(id=1), NSParent(id=2), NSParent(id=3), NSParent(id=4, deleted_at=deleted)])
    session.flush()
    # Parent 1 has an active and a soft-deleted child, parent 2 only a soft-deleted one, parent 3 none.
    session.add_all([
        NSChild(id=1, parent_id=1),
        NSChild(id=2, parent_id=1, deleted_at=deleted),
        NSChild(id=3, parent_id=2, deleted_at=deleted),
        NSChild(id=4, parent_id=4),
        NSOwner(id=1, parent_id=1),
        NSOwner(id=2, parent_id=2),
    ])
    session.flush()
    session.expunge_all()
    return session
