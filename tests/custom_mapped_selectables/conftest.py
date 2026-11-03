from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from tests.custom_mapped_selectables.model import MSEmployee, MSItem, MSManager, MSModelBase


@pytest.fixture
def db_session(db_connection: Connection) -> Session:
    MSModelBase.metadata.create_all(db_connection)  # type: ignore[attr-defined]
    session = sessionmaker(autocommit=False, autoflush=False, bind=db_connection)()
    erased = datetime.now(timezone.utc)
    session.add_all([
        MSEmployee(id=1, name="employee"),
        MSEmployee(id=2, name="erased employee", erased_at=erased),
        MSManager(id=3, name="manager"),
        MSManager(id=4, name="erased manager", erased_at=erased),
        MSItem(id=1, name="item"),
        MSItem(id=2, name="erased item", erased_at=erased),
    ])
    session.flush()
    session.expunge_all()
    return session
