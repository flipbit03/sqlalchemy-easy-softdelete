"""Eager loading into a class mapped to a SELECT of soft-deletable tables that leaves out the soft-delete column.
SQLAlchemy joins an alias of the SELECT that it makes while compiling a joined eager load, so the class's
loader criteria leave out the rows whose table row is soft-deleted, through its primary key."""

import pickle
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import aliased, contains_eager, joinedload, selectinload, subqueryload

from tests.eager_loading_partial_mapped_selects.model import PMChildName, PMParent

LOADERS = [joinedload, selectinload, subqueryload]
loaders = pytest.mark.parametrize("loader", LOADERS, ids=lambda loader: loader.__name__)

ACTIVE_CHILDREN = [(1, [1]), (2, [3])]


def _collections(parents: Any, attribute_name: str) -> list[tuple[int, list[int]]]:
    return [(parent.id, [child.id for child in getattr(parent, attribute_name)]) for parent in parents]


@loaders
@pytest.mark.parametrize("attribute_name", ["child_names", "children_with_parent"])
def test_eager_loading_into_a_class_mapped_to_a_select_without_the_column(db_session, loader, attribute_name):
    stmt = select(PMParent).options(loader(getattr(PMParent, attribute_name))).order_by(PMParent.id)

    for _ in range(2):
        db_session.expunge_all()
        assert _collections(db_session.scalars(stmt).unique(), attribute_name) == ACTIVE_CHILDREN


@loaders
def test_eager_loading_into_an_aliased_form_of_it(db_session, loader):
    stmt = select(PMParent).options(loader(PMParent.child_names.of_type(aliased(PMChildName)))).order_by(PMParent.id)

    assert _collections(db_session.scalars(stmt).unique(), "child_names") == ACTIVE_CHILDREN


@loaders
def test_eager_loading_next_to_a_join_to_an_aliased_form_of_it(db_session, loader):
    names = aliased(PMChildName)
    stmt = (
        select(PMParent, names.id)
        .join(names, names.parent_id == PMParent.id)
        .options(loader(PMParent.child_names))
        .order_by(names.id)
    )

    rows = [
        (parent.id, name_id, [child.id for child in parent.child_names])
        for parent, name_id in db_session.execute(stmt).unique()
    ]

    assert rows == [(1, 1, [1]), (2, 3, [3])]


@loaders
def test_eager_loading_from_it(db_session, loader):
    stmt = select(PMChildName).options(loader(PMChildName.parent)).order_by(PMChildName.id)

    rows = [(name.id, name.parent.id if name.parent else None) for name in db_session.scalars(stmt).unique()]

    # Child 4 is active; its parent is soft-deleted.
    assert rows == [(1, 1), (3, 2), (4, None)]


def test_joined_eager_loading_with_a_limit(db_session):
    stmt = select(PMParent).options(joinedload(PMParent.child_names)).order_by(PMParent.id).limit(1)

    assert _collections(db_session.scalars(stmt).unique(), "child_names") == [(1, [1])]


def test_contains_eager(db_session):
    stmt = (
        select(PMParent)
        .join(PMParent.child_names)
        .options(contains_eager(PMParent.child_names))
        .order_by(PMParent.id, PMChildName.id)
    )

    assert _collections(db_session.scalars(stmt).unique(), "child_names") == ACTIVE_CHILDREN


@loaders
def test_include_deleted(db_session, loader):
    stmt = (
        select(PMParent)
        .options(loader(PMParent.child_names))
        .order_by(PMParent.id)
        .execution_options(include_deleted=True)
    )

    assert _collections(db_session.scalars(stmt).unique(), "child_names") == [(1, [1, 2]), (2, [3]), (3, [4])]


@loaders
def test_pickled_eager_loaded_instances(db_session, loader):
    stmt = select(PMParent).options(loader(PMParent.child_names)).order_by(PMParent.id)
    parents = [pickle.loads(pickle.dumps(parent)) for parent in db_session.scalars(stmt).unique()]

    assert _collections(parents, "child_names") == ACTIVE_CHILDREN


@loaders
def test_lazy_loads_of_pickled_eager_loaded_instances(db_session, loader):
    """Unpickled instances keep the loader criteria of the query that loaded them, for their later loads."""
    stmt = select(PMParent).options(loader(PMParent.child_names), loader(PMParent.children_with_parent))
    parents = db_session.scalars(stmt.order_by(PMParent.id)).unique().all()
    for parent in parents:
        db_session.expire(parent, ["child_names", "children_with_parent"])
    restored = [db_session.merge(pickle.loads(pickle.dumps(parent)), load=False) for parent in parents]

    assert _collections(restored, "child_names") == ACTIVE_CHILDREN
    assert _collections(restored, "children_with_parent") == ACTIVE_CHILDREN


@pytest.mark.parametrize("loader", [selectinload, subqueryload], ids=lambda loader: loader.__name__)
def test_include_deleted_count_of_it_next_to_a_separate_eager_load(db_session, loader):
    """Only joined eager loads get the class's criteria, which SQLAlchemy would also apply to every SELECT of the
    class in the statement: selectin and subquery loads filter it as a FROM, and leave this count alone."""
    count = (
        select(func.count(PMChildName.id))
        .where(PMChildName.parent_id == PMParent.id)
        .execution_options(include_deleted=True)
        .scalar_subquery()
    )
    stmt = select(PMParent, count).options(loader(PMParent.child_names)).order_by(PMParent.id)

    rows = [(parent.id, n, [child.id for child in parent.child_names]) for parent, n in db_session.execute(stmt)]

    assert rows == [(1, 2, [1]), (2, 1, [3])]
