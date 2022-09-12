import datetime
import random

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from sqlalchemy_easy_softdelete.handler.rewriter import SoftDeleteQueryRewriter
from tests.model import SDChild, SDDerivedRequest, SDParent, TestModelBase

test_db_url = 'sqlite://'  # use in-memory database for tests


@pytest.fixture(scope="function")
def session_factory():
    engine = create_engine(test_db_url)
    TestModelBase.metadata.create_all(engine)

    yield sessionmaker(bind=engine)

    # SQLite in-memory db is deleted when its connection is closed.
    # https://www.sqlite.org/inmemorydb.html
    engine.dispose()


@pytest.fixture(scope="function")
def session(session_factory) -> Session:
    return session_factory()


def generate_parent_child_object_hierarchy(
    s: Session, parent_id: int, min_children: int = 1, max_children: int = 3, parent_deleted: bool = False
):
    # Fix a seed in the RNG for deterministic outputs
    random.seed(parent_id)

    # Generate the Parent
    deleted_at = datetime.datetime.utcnow() if parent_deleted else None
    new_parent = SDParent(id=parent_id, deleted_at=deleted_at)
    s.add(new_parent)
    s.flush()

    active_children = random.randint(min_children, max_children)

    # Add some active children
    for active_id in range(active_children):
        new_child = SDChild(id=parent_id * 1000 + active_id, parent=new_parent)
        s.add(new_child)
        s.flush()

    # Add some soft-deleted children
    for inactive_id in range(random.randint(min_children, max_children)):
        new_soft_deleted_child = SDChild(
            id=parent_id * 1000 + active_children + inactive_id,
            parent=new_parent,
            deleted_at=datetime.datetime.utcnow(),
        )
        s.add(new_soft_deleted_child)
        s.flush()

    s.commit()


def generate_table_with_inheritance_obj(s: Session, obj_id: int, deleted: bool = False):
    deleted_at = datetime.datetime.utcnow() if deleted else None
    new_parent = SDDerivedRequest(id=obj_id, deleted_at=deleted_at)
    s.add(new_parent)
    s.commit()


@pytest.fixture(scope="function")
def seeded_session(session) -> Session:
    generate_parent_child_object_hierarchy(session, 0)
    generate_parent_child_object_hierarchy(session, 1)
    generate_parent_child_object_hierarchy(session, 2, parent_deleted=True)

    generate_table_with_inheritance_obj(session, 0, deleted=False)
    generate_table_with_inheritance_obj(session, 1, deleted=False)
    generate_table_with_inheritance_obj(session, 2, deleted=True)
    return session


@pytest.fixture
def rewriter() -> SoftDeleteQueryRewriter:
    return SoftDeleteQueryRewriter("deleted_at", "include_deleted")
