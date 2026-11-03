"""Soft-delete filtering of aliased() entities, and of eager loading around a LIMIT.

- A subquery reached through ``aliased(Model, subquery)`` is filtered without modifying it.
- Joining an ``aliased()`` entity explicitly, while eager loading the same entity, renders valid SQL.
- With eager loading and a LIMIT, the ORM moves the statement into a subquery; the tables of the
  statement are still filtered.
"""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import false, func, select, union_all
from sqlalchemy.orm import Session, aliased, joinedload

from tests.default_config.model import SDChild, SDChildChild, SDParent

DELETED_PARENT_ID = 1002


def _active_child_ids(session: Session) -> list[int]:
    stmt = select(SDChild.id).where(SDChild.deleted_at.is_(None)).execution_options(include_deleted=True)
    return sorted(session.scalars(stmt))


def _active_child_ids_of_active_parents(session: Session) -> list[int]:
    stmt = (
        select(SDChild.id)
        .join(SDChild.parent)
        .where(SDChild.deleted_at.is_(None), SDParent.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    return sorted(session.scalars(stmt))


# Each case: (builds the aliased() entity, the expected child ids)
ALIASED_SUBQUERIES: dict[str, tuple[Callable[[], Any], Callable[[Session], list[int]]]] = {
    "orm_select": (lambda: aliased(SDChild, select(SDChild).subquery()), _active_child_ids),
    "core_select": (lambda: aliased(SDChild, select(SDChild.__table__).subquery()), _active_child_ids),
    "select_with_join": (
        lambda: aliased(SDChild, select(SDChild).join(SDChild.parent).subquery()),
        _active_child_ids_of_active_parents,
    ),
}


@pytest.mark.parametrize("make_alias, expected", ALIASED_SUBQUERIES.values(), ids=ALIASED_SUBQUERIES.keys())
def test_aliased_subquery_entity_is_filtered(seeded_session, make_alias, expected):
    alias = make_alias()

    ids = sorted(child.id for child in seeded_session.scalars(select(alias)))

    assert ids == expected(seeded_session)


def test_aliased_subquery_joined_with_onclause_is_filtered(seeded_session):
    alias = aliased(SDChild, select(SDChild).subquery())
    stmt = select(alias.id).join(SDParent, alias.parent_id == SDParent.id)

    assert sorted(seeded_session.scalars(stmt)) == _active_child_ids_of_active_parents(seeded_session)


def test_aliased_subquery_joined_through_relationship_is_filtered(seeded_session):
    alias = aliased(SDChild, select(SDChild).subquery())
    stmt = select(alias.id).select_from(SDParent).join(SDParent.children.of_type(alias))

    assert sorted(seeded_session.scalars(stmt)) == _active_child_ids_of_active_parents(seeded_session)


def test_include_deleted_inside_an_aliased_subquery_is_honored(seeded_session):
    alias = aliased(SDChild, select(SDChild).execution_options(include_deleted=True).subquery())

    ids = sorted(child.id for child in seeded_session.scalars(select(alias)))

    all_child_ids = sorted(seeded_session.scalars(select(SDChild.id).execution_options(include_deleted=True)))
    assert ids == all_child_ids


@pytest.mark.parametrize(
    "make_alias",
    [lambda: aliased(SDChild), lambda: aliased(SDChild, select(SDChild).subquery())],
    ids=["aliased_table", "aliased_subquery"],
)
def test_explicit_join_to_aliased_entity_with_eager_load_of_that_entity(seeded_session, make_alias):
    alias = make_alias()
    stmt = select(SDParent).join(alias, alias.parent_id == SDParent.id).options(joinedload(SDParent.children))

    parents = seeded_session.scalars(stmt).unique().all()

    assert parents
    assert all(parent.deleted_at is None for parent in parents)
    assert all(child.deleted_at is None for parent in parents for child in parent.children)


def test_eager_load_with_limit_filters_the_tables_joined_in_the_statement(seeded_session):
    stmt = select(SDChild).join(SDChild.parent).options(joinedload(SDChild.child_children)).limit(1000)

    children = seeded_session.scalars(stmt).unique().all()

    assert sorted(child.id for child in children) == _active_child_ids_of_active_parents(seeded_session)
    assert all(grandchild.deleted_at is None for child in children for grandchild in child.child_children)


def test_eager_load_with_limit_filters_the_queried_entity(seeded_session):
    stmt = select(SDParent).options(joinedload(SDParent.children)).order_by(SDParent.id.desc()).limit(1)

    parent = seeded_session.scalars(stmt).unique().one()

    assert parent.id != DELETED_PARENT_ID
    assert all(child.deleted_at is None for child in parent.children)


def test_query_first_with_eager_load_filters_the_queried_entity(seeded_session):
    parent = seeded_session.query(SDParent).options(joinedload(SDParent.children)).order_by(SDParent.id.desc()).first()

    assert parent is not None
    assert parent.id != DELETED_PARENT_ID


def test_eager_load_with_limit_of_a_child_entity_filters_grandchildren(seeded_session):
    stmt = select(SDChildChild).join(SDChildChild.child).options(joinedload(SDChildChild.child)).limit(1000)

    grandchildren = seeded_session.scalars(stmt).unique().all()

    assert grandchildren
    assert all(grandchild.deleted_at is None and grandchild.child.deleted_at is None for grandchild in grandchildren)


def _aliased_active_children() -> Any:
    return aliased(SDChild, select(SDChild).subquery())


NESTED_ALIASED_STATEMENTS: dict[str, Callable[[Session], Any]] = {
    "count_wrapper": lambda s: s.scalar(
        select(func.count()).select_from(select(_aliased_active_children()).subquery())
    ),
    "query_count": lambda s: s.query(_aliased_active_children()).count(),
    "union_member": lambda s: len(
        s.execute(union_all(select(_aliased_active_children().id), select(SDChild.id).where(false()))).all()
    ),
    "select_from_nested_select": lambda s: len(
        s.execute(select(select(_aliased_active_children().id).subquery())).all()
    ),
}


@pytest.mark.parametrize("count", NESTED_ALIASED_STATEMENTS.values(), ids=NESTED_ALIASED_STATEMENTS.keys())
def test_aliased_subquery_statement_nested_in_another_select_is_filtered(seeded_session, count):
    assert count(seeded_session) == len(_active_child_ids(seeded_session))


def test_core_tables_inside_an_aliased_subquery_are_filtered(seeded_session):
    child, parent = SDChild.__table__, SDParent.__table__
    inner = select(child).join(parent, parent.c.id == child.c.parent_id)
    alias = aliased(SDChild, inner.subquery())

    ids = sorted(row.id for row in seeded_session.scalars(select(alias)))

    assert ids == _active_child_ids_of_active_parents(seeded_session)


def test_union_with_core_member_inside_an_aliased_subquery_is_filtered(seeded_session):
    child = SDChild.__table__
    inner = union_all(select(SDChild).where(SDChild.parent_id == 1000), select(child).where(child.c.parent_id == 1001))
    alias = aliased(SDChild, inner.subquery())

    ids = sorted(row.id for row in seeded_session.scalars(select(alias)))

    expected = [i for i in _active_child_ids(seeded_session) if i // 100 in (1000, 1001)]
    assert ids == expected


def test_include_deleted_nested_inside_an_aliased_subquery_is_honored(seeded_session):
    child = SDChild.__table__
    everything = select(child).execution_options(include_deleted=True).subquery()
    alias = aliased(SDChild, select(everything).subquery())

    ids = sorted(row.id for row in seeded_session.scalars(select(alias)))

    all_child_ids = sorted(seeded_session.scalars(select(SDChild.id).execution_options(include_deleted=True)))
    assert ids == all_child_ids


def test_aliased_subquery_does_not_filter_other_selects_that_include_deleted(seeded_session):
    """The filter of an aliased() subquery stays inside it: a SELECT next to it that includes
    soft-deleted rows still includes them."""
    all_children = (
        select(SDChild.parent_id, func.count().label("n"))
        .group_by(SDChild.parent_id)
        .execution_options(include_deleted=True)
        .subquery()
    )
    alias = _aliased_active_children()
    stmt = (
        select(all_children.c.parent_id, all_children.c.n)
        .join(alias, alias.parent_id == all_children.c.parent_id)
        .distinct()
    )

    counts = sorted(seeded_session.execute(stmt).all())

    totals = sorted(
        seeded_session.execute(
            select(SDChild.parent_id, func.count()).group_by(SDChild.parent_id).execution_options(include_deleted=True)
        ).all()
    )
    assert counts == totals
