"""Soft-delete filtering of aliased() entities, and of eager loading around a LIMIT.

- A subquery reached through ``aliased(Model, subquery)`` is filtered without modifying it.
- Joining an ``aliased()`` entity explicitly, while eager loading the same entity, renders valid SQL.
- With eager loading and a LIMIT, the ORM moves the statement into a subquery; the tables of the
  statement are still filtered.
"""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
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


ALIASED_SUBQUERIES: dict[str, Callable[[], Any]] = {
    "orm_select": lambda: aliased(SDChild, select(SDChild).subquery()),
    "core_select": lambda: aliased(SDChild, select(SDChild.__table__).subquery()),
    "select_with_join": lambda: aliased(SDChild, select(SDChild).join(SDChild.parent).subquery()),
}


@pytest.mark.parametrize("make_alias", ALIASED_SUBQUERIES.values(), ids=ALIASED_SUBQUERIES.keys())
def test_aliased_subquery_entity_is_filtered(seeded_session, make_alias):
    alias = make_alias()

    ids = sorted(child.id for child in seeded_session.scalars(select(alias)))

    assert ids and set(ids) <= set(_active_child_ids(seeded_session))


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
    parent = (
        seeded_session.query(SDParent).options(joinedload(SDParent.children)).order_by(SDParent.id.desc()).first()
    )

    assert parent is not None
    assert parent.id != DELETED_PARENT_ID


def test_eager_load_with_limit_of_a_child_entity_filters_grandchildren(seeded_session):
    stmt = select(SDChildChild).join(SDChildChild.child).options(joinedload(SDChildChild.child)).limit(1000)

    grandchildren = seeded_session.scalars(stmt).unique().all()

    assert grandchildren
    assert all(grandchild.deleted_at is None and grandchild.child.deleted_at is None for grandchild in grandchildren)
