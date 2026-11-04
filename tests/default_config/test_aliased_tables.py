"""Aliases of soft-deletable tables are filtered on their own soft-delete column: ``aliased(Model)`` entities,
alone or joined, and the aliases that ``with_polymorphic(..., flat=True)`` and ``aliased(..., flat=True)``
join. The aliases are objects of each statement, so statements of the same shape built with new aliases
(sharing a remembered rewrite) are filtered too.
"""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased, with_polymorphic

from tests.default_config.model import SDBaseRequest, SDChild, SDDerivedRequest, SDParent


def _active_child_ids(session: Session) -> list[int]:
    stmt = select(SDChild.id).where(SDChild.deleted_at.is_(None)).execution_options(include_deleted=True)
    return sorted(session.scalars(stmt))


def _active_parent_child_pairs(session: Session) -> list[tuple[int, int]]:
    stmt = (
        select(SDParent.id, SDChild.id)
        .join(SDChild, SDChild.parent_id == SDParent.id)
        .where(SDParent.deleted_at.is_(None), SDChild.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    return sorted((parent_id, child_id) for parent_id, child_id in session.execute(stmt))


def test_select_from_an_aliased_table(seeded_session):
    seeded_session.expunge_all()
    child = aliased(SDChild)

    children = seeded_session.scalars(select(child)).all()

    assert sorted(c.id for c in children) == _active_child_ids(seeded_session)


JOINS_TO_AN_ALIASED_TABLE: dict[str, Callable[[Any], Any]] = {
    "relationship": lambda child: select(SDParent.id, child.id).join(SDParent.children.of_type(child)),
    "onclause": lambda child: select(SDParent.id, child.id).join(child, child.parent_id == SDParent.id),
    "join_from": lambda child: select(SDParent.id, child.id).join_from(child, child.parent),
}


@pytest.mark.parametrize("make_stmt", JOINS_TO_AN_ALIASED_TABLE.values(), ids=JOINS_TO_AN_ALIASED_TABLE.keys())
def test_join_with_an_aliased_table(seeded_session, make_stmt):
    expected = _active_parent_child_pairs(seeded_session)

    # New aliases each time: statements of the same shape share their remembered rewrite.
    for _ in range(3):
        rows = seeded_session.execute(make_stmt(aliased(SDChild))).all()

        assert sorted(tuple(row) for row in rows) == expected


def test_aliased_table_joined_to_its_relationship(seeded_session):
    child = aliased(SDChild)

    ids = seeded_session.scalars(select(child.id).join(child.parent)).all()

    assert sorted(ids) == sorted(child_id for _, child_id in _active_parent_child_pairs(seeded_session))


def test_join_of_two_aliases_of_a_table(seeded_session):
    left, right = aliased(SDChild), aliased(SDChild)
    stmt = select(left.id, right.id).join(right, (right.parent_id == left.parent_id) & (right.id != left.id))

    pairs = sorted(tuple(row) for row in seeded_session.execute(stmt))

    active = set(_active_child_ids(seeded_session))
    all_children = seeded_session.execute(select(SDChild.id, SDChild.parent_id).execution_options(include_deleted=True))
    parent_of = dict(tuple(row) for row in all_children)
    expected = sorted((a, b) for a in active for b in active if a != b and parent_of[a] == parent_of[b])
    assert pairs
    assert pairs == expected


def _active_request_ids(session: Session) -> list[int]:
    stmt = select(SDBaseRequest.id).where(SDBaseRequest.deleted_at.is_(None)).execution_options(include_deleted=True)
    return sorted(session.scalars(stmt))


@pytest.mark.parametrize(
    "make_entity",
    [
        lambda: with_polymorphic(SDBaseRequest, [SDDerivedRequest], flat=True),
        lambda: aliased(SDDerivedRequest, flat=True),
    ],
    ids=["with_polymorphic_flat", "aliased_flat"],
)
def test_flat_aliases_of_an_inheritance_hierarchy(seeded_session, make_entity):
    seeded_session.expunge_all()

    for _ in range(2):
        requests = seeded_session.scalars(select(make_entity())).all()

        assert sorted(request.id for request in requests) == _active_request_ids(seeded_session)


def test_include_deleted_on_a_statement_with_an_aliased_table(seeded_session):
    child = aliased(SDChild)

    ids = seeded_session.scalars(select(child.id).execution_options(include_deleted=True)).all()

    assert sorted(ids) == sorted(seeded_session.scalars(select(SDChild.id).execution_options(include_deleted=True)))


def test_alias_of_an_alias_of_a_table(seeded_session):
    nested = SDChild.__table__.alias().alias()
    stmt = select(SDParent.id, nested.c.id).join(nested, nested.c.parent_id == SDParent.id)

    rows = seeded_session.execute(stmt).all()

    assert sorted((parent_id, child_id) for parent_id, child_id in rows) == _active_parent_child_pairs(seeded_session)


def test_aliased_entity_over_an_alias_of_an_alias_of_a_table(seeded_session):
    seeded_session.expunge_all()
    child = aliased(SDChild, alias=SDChild.__table__.alias().alias())

    children = seeded_session.scalars(select(child)).all()

    assert sorted(c.id for c in children) == _active_child_ids(seeded_session)
