from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from tests.secondary_subqueries.model import SSChild, SSGrandChild, SSModelBase, SSParent


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    SSModelBase.metadata.create_all(db_connection)
    session = sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()
    session.add(SSParent(id=1))
    session.flush()
    # Child 2 is soft-deleted: its grandchild 20 does not belong to parent 1 any more.
    session.add_all([SSChild(id=1, parent_id=1), SSChild(id=2, parent_id=1, deleted_at=datetime.now(timezone.utc))])
    session.flush()
    session.add_all([SSGrandChild(id=10, child_id=1), SSGrandChild(id=20, child_id=2)])
    session.flush()
    session.expunge_all()
    return session
