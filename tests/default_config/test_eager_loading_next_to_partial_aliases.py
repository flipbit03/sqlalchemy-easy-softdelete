"""Eager loading a class keeps its loader criteria off the ``aliased()`` entities of that class over subqueries
that leave out the soft-delete column, wherever the statement refers to them: in an ON clause, as the
``of_type()`` entity of a relationship join, or in the path of a loader option. The criteria would name the
class's own table there ("no such column", or a cartesian product with it)."""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased, joinedload, selectinload

from tests.default_config.model import SDChild, SDParent


def _partial_children() -> Any:
    """An aliased() subquery of SDChild without its soft-delete column."""
    return aliased(SDChild, select(SDChild.id, SDChild.parent_id).subquery())


def _active_children_by_parent(session: Session) -> dict[int, list[int]]:
    stmt = (
        select(SDParent.id, SDChild.id)
        .join(SDChild, SDChild.parent_id == SDParent.id)
        .where(SDParent.deleted_at.is_(None), SDChild.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    by_parent: dict[int, list[int]] = {}
    for parent_id, child_id in session.execute(stmt):
        by_parent.setdefault(parent_id, []).append(child_id)
    return {parent_id: sorted(ids) for parent_id, ids in by_parent.items()}


JOINS_TO_A_PARTIAL_ALIAS: dict[str, Callable[[Any], Any]] = {
    "onclause": lambda partial: select(SDParent).join(partial, partial.parent_id == SDParent.id),
    "of_type": lambda partial: select(SDParent).join(SDParent.children.of_type(partial)),
}


@pytest.mark.parametrize("loader", [joinedload, selectinload], ids=lambda loader: loader.__name__)
@pytest.mark.parametrize("make_stmt", JOINS_TO_A_PARTIAL_ALIAS.values(), ids=JOINS_TO_A_PARTIAL_ALIAS.keys())
def test_eager_load_next_to_a_join_to_a_partial_alias(seeded_session, make_stmt, loader):
    seeded_session.expunge_all()
    stmt = make_stmt(_partial_children()).options(loader(SDParent.children))

    parents = seeded_session.scalars(stmt).unique().all()
    loaded = {parent.id: sorted(child.id for child in parent.children) for parent in parents}

    assert loaded == _active_children_by_parent(seeded_session)


def test_selectin_load_into_a_partial_alias(seeded_session):
    seeded_session.expunge_all()
    partial = _partial_children()

    parents = seeded_session.scalars(select(SDParent).options(selectinload(SDParent.children.of_type(partial))))
    loaded = {parent.id: sorted(child.id for child in parent.children) for parent in parents}

    assert loaded == _active_children_by_parent(seeded_session)
