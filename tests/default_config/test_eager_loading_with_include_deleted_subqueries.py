"""Eager loading a class filters it in the statement's SELECTs with loader criteria, but leaves alone the SELECTs
that ask for soft-deleted rows (``include_deleted``), such as a subquery counting every child of a parent."""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import bindparam, func, select, union_all
from sqlalchemy.orm import Session, aliased, joinedload, selectinload, subqueryload

from tests.default_config.model import SDChild, SDParent

LOADERS = [joinedload, selectinload, subqueryload]


def _all_children_counts(session: Session) -> list[tuple[int, int]]:
    """Each active parent with the number of its children, soft-deleted ones included."""
    stmt = (
        select(SDParent.id, func.count(SDChild.id))
        .join(SDChild, SDChild.parent_id == SDParent.id)
        .where(SDParent.deleted_at.is_(None))
        .group_by(SDParent.id)
        .execution_options(include_deleted=True)
    )
    return sorted((parent_id, n) for parent_id, n in session.execute(stmt))


def _counts_subquery(include_deleted: bool = True) -> Any:
    counts = select(SDChild.parent_id, func.count().label("n")).group_by(SDChild.parent_id)
    if include_deleted:
        counts = counts.execution_options(include_deleted=True)
    return counts.subquery()


def _counts_through_an_aliased_subquery() -> Any:
    everything = aliased(SDChild, select(SDChild).execution_options(include_deleted=True).subquery())
    return everything


STATEMENTS: dict[str, Callable[[Any], Any]] = {
    "subquery": lambda loader: (
        lambda counts: select(SDParent, counts.c.n).join(counts, counts.c.parent_id == SDParent.id)
    )(_counts_subquery()).options(loader(SDParent.children)),
    "aliased_subquery": lambda loader: (
        lambda everything: (
            select(SDParent, func.count(everything.id))
            .join(everything, everything.parent_id == SDParent.id)
            .group_by(SDParent.id)
        )
    )(_counts_through_an_aliased_subquery()).options(loader(SDParent.children)),
}


@pytest.mark.parametrize("loader", LOADERS, ids=lambda loader: loader.__name__)
@pytest.mark.parametrize("make_stmt", STATEMENTS.values(), ids=STATEMENTS.keys())
def test_eager_loading_leaves_include_deleted_subqueries_alone(seeded_session, make_stmt, loader):
    seeded_session.expunge_all()

    rows = seeded_session.execute(make_stmt(loader)).unique().all()

    assert sorted((parent.id, n) for parent, n in rows) == _all_children_counts(seeded_session)
    assert all(child.deleted_at is None for parent, _ in rows for child in parent.children)


@pytest.mark.parametrize("loader", LOADERS, ids=lambda loader: loader.__name__)
def test_statements_of_the_same_shape_with_and_without_include_deleted(seeded_session, loader):
    """The subquery that asks for soft-deleted rows and the one that does not take turns, through the rewrite's
    and SQLAlchemy's caches."""
    active_counts = sorted(
        (parent_id, n)
        for parent_id, n in seeded_session.execute(
            select(SDParent.id, func.count(SDChild.id))
            .join(SDChild, SDChild.parent_id == SDParent.id)
            .where(SDParent.deleted_at.is_(None), SDChild.deleted_at.is_(None))
            .group_by(SDParent.id)
            .execution_options(include_deleted=True)
        )
    )
    for include_deleted in (True, False, True, False):
        seeded_session.expunge_all()
        counts = _counts_subquery(include_deleted)
        stmt = (
            select(SDParent, counts.c.n)
            .join(counts, counts.c.parent_id == SDParent.id)
            .options(loader(SDParent.children))
        )

        rows = sorted((parent.id, n) for parent, n in seeded_session.execute(stmt).unique())

        assert rows == (_all_children_counts(seeded_session) if include_deleted else active_counts)


def _active_children_counts(session: Session) -> list[tuple[int, int]]:
    stmt = (
        select(SDParent.id, func.count(SDChild.id))
        .join(SDChild, SDChild.parent_id == SDParent.id)
        .where(SDParent.deleted_at.is_(None), SDChild.deleted_at.is_(None))
        .group_by(SDParent.id)
        .execution_options(include_deleted=True)
    )
    return sorted((parent_id, n) for parent_id, n in session.execute(stmt))


@pytest.mark.parametrize("loader", LOADERS, ids=lambda loader: loader.__name__)
def test_scalar_subqueries_of_the_same_shape_do_not_share_their_filtering(seeded_session, loader):
    """A scalar subquery gets no filtered copy, so statements of the same shape with and without
    include_deleted on it share their compiled SQL: the statement without it never counts soft-deleted rows."""
    results: dict[bool, list[Any]] = {True: [], False: []}
    for include_deleted in (True, False, True, False, False):
        seeded_session.expunge_all()
        count = select(func.count(SDChild.id)).where(SDChild.parent_id == SDParent.id)
        if include_deleted:
            count = count.execution_options(include_deleted=True)
        stmt = select(SDParent, count.scalar_subquery()).options(loader(SDParent.children))

        results[include_deleted].append(sorted((parent.id, n) for parent, n in seeded_session.execute(stmt).unique()))

    assert all(rows == _active_children_counts(seeded_session) for rows in results[False])
    assert all(rows == results[True][0] for rows in results[True])


@pytest.mark.parametrize("loader", [joinedload, selectinload], ids=lambda loader: loader.__name__)
def test_clone_of_an_include_deleted_subquery_that_filters_again(seeded_session, loader):
    """include_deleted=False on a clone of a SELECT that includes soft-deleted rows (.params() clones it)."""
    seeded_session.expunge_all()
    counts = (
        select(SDChild.parent_id, func.count().label("n"))
        .where(SDChild.id > bindparam("min_id", 0))
        .group_by(SDChild.parent_id)
        .execution_options(include_deleted=True)
    )
    filtered_again = counts.params(min_id=0).execution_options(include_deleted=False).subquery()
    stmt = (
        select(SDParent, filtered_again.c.n)
        .join(filtered_again, filtered_again.c.parent_id == SDParent.id)
        .options(loader(SDParent.children))
    )

    rows = sorted((parent.id, n) for parent, n in seeded_session.execute(stmt).unique())

    assert rows == _active_children_counts(seeded_session)


@pytest.mark.parametrize("loader", [joinedload, selectinload], ids=lambda loader: loader.__name__)
def test_selects_nested_in_an_include_deleted_subquery(seeded_session, loader):
    """A union member, or a subquery, inside a SELECT that includes soft-deleted rows includes them too."""
    seeded_session.expunge_all()
    children = select(SDChild).subquery()
    counts = (
        select(children.c.parent_id, func.count().label("n"))
        .group_by(children.c.parent_id)
        .execution_options(include_deleted=True)
        .subquery()
    )
    stmt = (
        select(SDParent, counts.c.n).join(counts, counts.c.parent_id == SDParent.id).options(loader(SDParent.children))
    )

    rows = sorted((parent.id, n) for parent, n in seeded_session.execute(stmt).unique())

    assert rows == _all_children_counts(seeded_session)


@pytest.mark.parametrize("loader", [joinedload, selectinload], ids=lambda loader: loader.__name__)
def test_union_inside_an_include_deleted_subquery(seeded_session, loader):
    seeded_session.expunge_all()
    both = (
        union_all(select(SDChild.parent_id), select(SDChild.parent_id))
        .execution_options(include_deleted=True)
        .subquery()
    )
    counts = select(both.c.parent_id, func.count().label("n")).group_by(both.c.parent_id).subquery()
    stmt = (
        select(SDParent, counts.c.n).join(counts, counts.c.parent_id == SDParent.id).options(loader(SDParent.children))
    )

    rows = sorted((parent.id, n) for parent, n in seeded_session.execute(stmt).unique())

    assert rows == [(parent_id, 2 * n) for parent_id, n in _all_children_counts(seeded_session)]
