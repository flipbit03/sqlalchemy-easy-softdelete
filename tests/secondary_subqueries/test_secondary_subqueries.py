"""Soft-deleted rows inside a relationship's secondary subquery do not link related rows. SQLAlchemy joins an
alias of the subquery that it makes while compiling a relationship join, so the rewrite filters it with
``relationship.and_()``: on the soft-delete column the subquery passes on, or through the primary key of its
table. SQLAlchemy adapts those criteria to its alias from 2.0.28 on."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from sqlalchemy_easy_softdelete.handler.rewriter import SECONDARY_CRITERIA_SUPPORTED
from tests.secondary_subqueries.model import SSGrandChild, SSParent

needs_secondary_criteria = pytest.mark.skipif(
    not SECONDARY_CRITERIA_SUPPORTED,
    reason="SQLAlchemy before 2.0.28 does not adapt relationship.and_() criteria to the secondary's alias",
)

RELATIONSHIPS = ["grandchildren", "grandchildren_by_rows"]


@needs_secondary_criteria
@pytest.mark.parametrize("relationship_name", RELATIONSHIPS)
@pytest.mark.parametrize("outer", [False, True], ids=["join", "outerjoin"])
def test_join_through_a_secondary_subquery(db_session, relationship_name, outer):
    attribute = getattr(SSParent, relationship_name)
    stmt = select(SSParent.id, SSGrandChild.id)
    stmt = stmt.outerjoin(attribute) if outer else stmt.join(attribute)

    for _ in range(2):
        assert db_session.execute(stmt).all() == [(1, 10)]


@needs_secondary_criteria
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
