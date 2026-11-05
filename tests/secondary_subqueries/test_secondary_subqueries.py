"""Soft-deleted rows inside a relationship's secondary subquery do not link related rows. SQLAlchemy joins an
alias of the subquery that it makes while compiling a relationship join, so the rewrite filters it with
``relationship.and_()``: on the soft-delete column the subquery passes on, or through the primary key of its
table. SQLAlchemy adapts those criteria to its alias from 2.0.28 on."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from tests.secondary_subqueries.model import SSChild, SSGrandChild, SSNickname, SSParent

RELATIONSHIPS = ["grandchildren", "grandchildren_by_rows"]


@pytest.mark.parametrize("relationship_name", RELATIONSHIPS)
@pytest.mark.parametrize("outer", [False, True], ids=["join", "outerjoin"])
def test_join_through_a_secondary_subquery(db_session, relationship_name, outer):
    attribute = getattr(SSParent, relationship_name)
    stmt = select(SSParent.id, SSGrandChild.id)
    stmt = stmt.outerjoin(attribute) if outer else stmt.join(attribute)

    for _ in range(2):
        assert db_session.execute(stmt).all() == [(1, 10)]


@pytest.mark.parametrize("relationship_name", RELATIONSHIPS)
def test_selectin_load_through_a_secondary_subquery(db_session, relationship_name):
    attribute = getattr(SSParent, relationship_name)

    parent = db_session.scalars(select(SSParent).options(selectinload(attribute))).one()

    assert [grandchild.id for grandchild in getattr(parent, relationship_name)] == [10]


@pytest.mark.parametrize("relationship_name", RELATIONSHIPS)
def test_lazy_load_through_a_secondary_subquery(db_session, relationship_name):
    parent = db_session.get(SSParent, 1)

    assert [grandchild.id for grandchild in getattr(parent, relationship_name)] == [10]


def test_secondary_subquery_that_includes_deleted_rows(db_session):
    stmt = select(SSParent.id, SSGrandChild.id).join(SSParent.all_grandchildren).order_by(SSGrandChild.id)

    assert db_session.execute(stmt).all() == [(1, 10), (1, 20)]


def test_include_deleted_on_a_join_through_a_secondary_subquery(db_session):
    stmt = (
        select(SSParent.id, SSGrandChild.id)
        .join(SSParent.grandchildren)
        .order_by(SSGrandChild.id)
        .execution_options(include_deleted=True)
    )

    assert db_session.execute(stmt).all() == [(1, 10), (1, 20)]


@pytest.fixture
def nicknames(db_session):
    """Child 1 has no nickname, child 3 an active one, child 4 a soft-deleted one."""
    db_session.add_all([SSChild(id=3, parent_id=1), SSChild(id=4, parent_id=1)])
    db_session.flush()
    db_session.add_all([
        SSNickname(id=1, child_id=3),
        SSNickname(id=2, child_id=4, deleted_at=datetime.now(timezone.utc)),
        SSGrandChild(id=30, child_id=3),
        SSGrandChild(id=40, child_id=4),
    ])
    db_session.flush()
    db_session.expunge_all()
    return db_session


@pytest.mark.parametrize("outer", [False, True], ids=["join", "outerjoin"])
def test_join_through_a_secondary_subquery_that_outer_joins_a_table(nicknames, outer):
    """A child without a nickname, or with a soft-deleted one, still links its grandchildren, as in the lazy load:
    the subquery outer joins the nicknames, so a soft-deleted nickname leaves NULLs, as if it did not exist."""
    stmt = select(SSGrandChild.id).order_by(SSGrandChild.id)
    stmt = (
        stmt.outerjoin(SSParent.grandchildren_by_nickname) if outer else stmt.join(SSParent.grandchildren_by_nickname)
    )
    stmt = stmt.select_from(SSParent)

    assert nicknames.scalars(stmt).all() == [10, 30, 40]
    assert [grandchild.id for grandchild in nicknames.get(SSParent, 1).grandchildren_by_nickname] == [10, 30, 40]
