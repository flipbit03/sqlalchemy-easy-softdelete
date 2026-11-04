from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from tests.secondary_tables.model import STLink, STModelBase, STParent, STTag


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    STModelBase.metadata.create_all(db_connection)
    session = sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()
    session.add_all([STParent(id=1), STTag(id=10), STTag(id=20)])
    session.flush()
    # The link between parent 1 and tag 20 is soft-deleted.
    session.add_all([
        STLink(parent_id=1, tag_id=10),
        STLink(parent_id=1, tag_id=20, deleted_at=datetime.now(timezone.utc)),
    ])
    session.flush()
    session.expunge_all()
    return session
