from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from tests.of_type_subclasses.model import (
    OTChild,
    OTItem,
    OTLeafChild,
    OTModelBase,
    OTParent,
    OTSpecialChild,
    OTSpecialItem,
)


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    OTModelBase.metadata.create_all(db_connection)
    session = sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()
    deleted = datetime.now(timezone.utc)
    session.add(OTParent(id=1))
    session.flush()
    # Rows 2 and 4 of each hierarchy are soft-deleted; rows 3 and 4 are of the subclass.
    session.add_all([
        OTChild(id=1, parent_id=1),
        OTChild(id=2, parent_id=1, deleted_at=deleted),
        OTSpecialChild(id=3, parent_id=1),
        OTSpecialChild(id=4, parent_id=1, deleted_at=deleted),
        OTItem(id=1, parent_id=1),
        OTItem(id=2, parent_id=1, deleted_at=deleted),
        OTSpecialItem(id=3, parent_id=1),
        OTSpecialItem(id=4, parent_id=1, deleted_at=deleted),
    ])
    session.flush()
    # A third level of the single-table hierarchy: 6 is soft-deleted.
    session.add_all([OTLeafChild(id=5, parent_id=1), OTLeafChild(id=6, parent_id=1, deleted_at=deleted)])
    session.flush()
    session.expunge_all()
    return session
