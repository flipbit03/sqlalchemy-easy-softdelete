from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from tests.joined_secondaries.model import (
    JSEagerParent,
    JSModelBase,
    JSOwner,
    JSParent,
    JSSpecialItem,
    JSTag,
    item_link,
    link,
)


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    JSModelBase.metadata.create_all(db_connection)
    session = sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()
    session.add(JSOwner(id=1))
    session.flush()
    session.add_all([
        JSParent(id=1, owner_id=1),
        JSEagerParent(id=1, owner_id=1),
        JSSpecialItem(id=1, owner_id=1),
        JSTag(id=10),
        JSTag(id=20),
    ])
    session.flush()
    # The link to tag 20 is soft-deleted.
    session.execute(
        link.insert(),
        [
            {"parent_id": 1, "tag_id": 10, "deleted_at": None},
            {"parent_id": 1, "tag_id": 20, "deleted_at": datetime.now(timezone.utc)},
        ],
    )
    session.execute(
        item_link.insert(),
        [
            {"item_id": 1, "tag_id": 10, "deleted_at": None},
            {"item_id": 1, "tag_id": 20, "deleted_at": datetime.now(timezone.utc)},
        ],
    )
    session.flush()
    session.expunge_all()
    return session
