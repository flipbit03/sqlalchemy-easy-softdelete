"""``include_deleted`` given at execution time, instead of on the statement, disables soft-delete filtering
like the statement's own option: in ``Session.execute()``, ``Session.scalars()`` and ``Session.get()``, and in
the eager loads that the execution runs. Lazy loads run later, on their own, and stay filtered.
"""

from collections import defaultdict

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, immediateload, joinedload, selectinload, subqueryload

from tests.default_config.model import SDChild, SDParent

INCLUDE_DELETED = {"include_deleted": True}


def _all_child_ids(session: Session) -> list[int]:
    return sorted(session.scalars(select(SDChild.id).execution_options(include_deleted=True)))


def _all_child_ids_by_parent(session: Session) -> dict[int, list[int]]:
    stmt = select(SDChild.parent_id, SDChild.id).execution_options(include_deleted=True)
    by_parent: dict[int, list[int]] = defaultdict(list)
    for parent_id, child_id in session.execute(stmt):
        by_parent[parent_id].append(child_id)
    return {parent_id: sorted(child_ids) for parent_id, child_ids in by_parent.items()}


def _a_deleted_child_id(session: Session) -> int:
    stmt = select(SDChild.id).where(SDChild.deleted_at.is_not(None)).execution_options(include_deleted=True)
    return session.scalars(stmt.order_by(SDChild.id).limit(1)).one()


def test_scalars_with_include_deleted_at_execution_time(seeded_session):
    seeded_session.expunge_all()

    children = seeded_session.scalars(select(SDChild), execution_options=INCLUDE_DELETED).all()

    assert sorted(child.id for child in children) == _all_child_ids(seeded_session)


def test_execute_with_include_deleted_at_execution_time(seeded_session):
    child_ids = seeded_session.execute(select(SDChild.id), execution_options=INCLUDE_DELETED).scalars().all()

    assert sorted(child_ids) == _all_child_ids(seeded_session)


def test_get_with_include_deleted_at_execution_time(seeded_session):
    deleted_id = _a_deleted_child_id(seeded_session)
    seeded_session.expunge_all()
    assert seeded_session.get(SDChild, deleted_id) is None

    seeded_session.expunge_all()
    child = seeded_session.get(SDChild, deleted_id, execution_options=INCLUDE_DELETED)

    assert child is not None
    assert child.id == deleted_id
    assert child.deleted_at is not None


@pytest.mark.parametrize(
    "loader", [joinedload, selectinload, subqueryload, immediateload], ids=lambda loader: loader.__name__
)
@pytest.mark.parametrize("given_on", ["statement", "execution"])
def test_eager_loads_of_an_include_deleted_execution_include_deleted_children(seeded_session, loader, given_on):
    expected = _all_child_ids_by_parent(seeded_session)
    seeded_session.expunge_all()

    stmt = select(SDParent).options(loader(SDParent.children))
    if given_on == "statement":
        parents = seeded_session.scalars(stmt.execution_options(include_deleted=True))
    else:
        parents = seeded_session.scalars(stmt, execution_options=INCLUDE_DELETED)
    loaded = {parent.id: sorted(child.id for child in parent.children) for parent in parents.unique()}

    assert loaded == expected


@pytest.mark.parametrize("given_on", ["statement", "execution"])
def test_lazy_loads_after_an_include_deleted_execution_are_filtered(seeded_session, given_on):
    seeded_session.expunge_all()

    stmt = select(SDParent)
    if given_on == "statement":
        parents = seeded_session.scalars(stmt.execution_options(include_deleted=True)).all()
    else:
        parents = seeded_session.scalars(stmt, execution_options=INCLUDE_DELETED).all()
    children = [child for parent in parents for child in parent.children]

    assert any(parent.deleted_at is not None for parent in parents)
    assert children
    assert all(child.deleted_at is None for child in children)
