"""``select(...).from_statement(union(...))`` executed through the session filters each member of the union,
and honors ``include_deleted`` on the ``from_statement()`` and on each member."""

import pytest
from sqlalchemy import lambda_stmt, select, text, union, union_all
from sqlalchemy.orm import Session

from tests.default_config.model import SDChild, SDParent


def _child_ids(session: Session, parent_id: int, *, active_only: bool) -> list[int]:
    stmt = select(SDChild.id).where(SDChild.parent_id == parent_id).execution_options(include_deleted=True)
    if active_only:
        stmt = stmt.where(SDChild.deleted_at.is_(None))
    return sorted(session.scalars(stmt))


def _member(parent_id: int):
    return select(SDChild).where(SDChild.parent_id == parent_id)


@pytest.mark.parametrize("compound", [union_all, union], ids=["union_all", "union"])
def test_from_statement_of_a_union_is_filtered(seeded_session, compound):
    stmt = select(SDChild).from_statement(compound(_member(1000), _member(1001)))

    children = seeded_session.scalars(stmt).all()

    expected = _child_ids(seeded_session, 1000, active_only=True) + _child_ids(seeded_session, 1001, active_only=True)
    assert sorted(child.id for child in children) == sorted(expected)


def test_from_statement_of_a_union_with_include_deleted_is_not_filtered(seeded_session):
    stmt = (
        select(SDChild).from_statement(union_all(_member(1000), _member(1001))).execution_options(include_deleted=True)
    )

    children = seeded_session.scalars(stmt).all()

    expected = _child_ids(seeded_session, 1000, active_only=False) + _child_ids(seeded_session, 1001, active_only=False)
    assert sorted(child.id for child in children) == sorted(expected)


def test_from_statement_of_a_union_honors_include_deleted_on_a_member(seeded_session):
    members = union_all(_member(1000).execution_options(include_deleted=True), _member(1001))
    stmt = select(SDChild).from_statement(members)

    children = seeded_session.scalars(stmt).all()

    expected = _child_ids(seeded_session, 1000, active_only=False) + _child_ids(seeded_session, 1001, active_only=True)
    assert sorted(child.id for child in children) == sorted(expected)


def test_from_statement_of_a_union_of_parents_is_filtered(seeded_session):
    stmt = select(SDParent).from_statement(
        union_all(select(SDParent).where(SDParent.id <= 1001), select(SDParent).where(SDParent.id > 1001))
    )

    parents = seeded_session.scalars(stmt).all()

    assert sorted(parent.id for parent in parents) == [1000, 1001]


def test_lambda_stmt_of_a_from_statement_of_a_union_is_filtered(seeded_session):
    stmt = lambda_stmt(lambda: select(SDChild).from_statement(union_all(_member(1000), _member(1001))))

    for _ in range(2):
        children = seeded_session.scalars(stmt).all()

        expected = _child_ids(seeded_session, 1000, active_only=True) + _child_ids(
            seeded_session, 1001, active_only=True
        )
        assert sorted(child.id for child in children) == sorted(expected)


# The columns of select(SDChild), in its order: a union matches its members' columns by position.
TEXTUAL_CHILDREN = text("SELECT parent_id, id, deleted_at FROM sdchild WHERE parent_id = 1001").columns(
    SDChild.parent_id, SDChild.id, SDChild.deleted_at
)


def test_union_with_a_textual_member_filters_the_other_members(seeded_session):
    stmt = union_all(_member(1000), TEXTUAL_CHILDREN)

    ids = [row.id for row in seeded_session.execute(stmt)]

    expected = _child_ids(seeded_session, 1000, active_only=True) + _child_ids(seeded_session, 1001, active_only=False)
    assert sorted(ids) == sorted(expected)


def test_from_statement_of_a_union_with_a_textual_member_filters_the_other_members(seeded_session):
    stmt = select(SDChild).from_statement(union_all(_member(1000), TEXTUAL_CHILDREN))

    children = seeded_session.scalars(stmt).all()

    expected = _child_ids(seeded_session, 1000, active_only=True) + _child_ids(seeded_session, 1001, active_only=False)
    assert sorted(child.id for child in children) == sorted(expected)


def test_from_statement_of_textual_sql_is_left_as_it_is(seeded_session):
    stmt = select(SDChild).from_statement(TEXTUAL_CHILDREN)

    children = seeded_session.scalars(stmt).all()

    assert sorted(child.id for child in children) == _child_ids(seeded_session, 1001, active_only=False)
