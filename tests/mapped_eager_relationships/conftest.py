from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from tests.mapped_eager_relationships.model import (
    MEArchive,
    MEChild,
    MEHolder,
    MEInnerChild,
    MEInnerParent,
    MEManager,
    MEModelBase,
    MENode,
    MEParent,
    MEReport,
    METoy,
)


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    MEModelBase.metadata.create_all(db_connection)
    session = sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()
    deleted = datetime.now(timezone.utc)
    session.add_all([MEParent(id=1), MEParent(id=2), MEInnerParent(id=1), MEInnerParent(id=2), MENode(id=1)])
    session.flush()
    # Parent 2's only child is soft-deleted; so is inner parent 2's.
    session.add_all([
        MEChild(id=1, parent_id=1),
        MEChild(id=2, parent_id=1, deleted_at=deleted),
        MEChild(id=3, parent_id=2, deleted_at=deleted),
        MEInnerChild(id=1, parent_id=1),
        MEInnerChild(id=2, parent_id=1, deleted_at=deleted),
        MEInnerChild(id=3, parent_id=2, deleted_at=deleted),
        MENode(id=2, parent_id=1),
        MENode(id=3, parent_id=1, deleted_at=deleted),
        MEHolder(id=1, parent_id=1),
        MEManager(id=1),
        MEArchive(id=1),
        MEArchive(id=2),
    ])
    session.flush()
    session.add_all([
        METoy(id=1, child_id=1),
        METoy(id=2, child_id=1, deleted_at=deleted),
        MENode(id=4, parent_id=2),
        MENode(id=5, parent_id=2, deleted_at=deleted),
        MEReport(id=1, manager_id=1),
        MEReport(id=2, manager_id=1, deleted_at=deleted),
    ])
    session.flush()
    session.expunge_all()
    return session
