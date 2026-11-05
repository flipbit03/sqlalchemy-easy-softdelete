from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from tests.outer_joined_subqueries.model import OJBadge, OJChild, OJModelBase, OJNickname, OJParent


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    OJModelBase.metadata.create_all(db_connection)
    session = sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()
    deleted = datetime.now(timezone.utc)
    session.add(OJParent(id=1))
    session.flush()
    # Child 1 has no nickname, child 2 is soft-deleted, child 3 has an active nickname, child 4 a soft-deleted one.
    session.add_all([
        OJChild(id=1, parent_id=1),
        OJChild(id=2, parent_id=1, deleted_at=deleted),
        OJChild(id=3, parent_id=1),
        OJChild(id=4, parent_id=1),
    ])
    session.flush()
    session.add_all([OJNickname(id=1, child_id=3), OJNickname(id=2, child_id=4, deleted_at=deleted)])
    session.flush()
    # Badge 20 belongs to the soft-deleted nickname.
    session.add_all([OJBadge(id=10, nickname_id=1), OJBadge(id=20, nickname_id=2)])
    session.flush()
    session.expunge_all()
    return session
