"""Soft-deleted rows of a relationship's secondary table do not link related rows. SQLAlchemy joins an alias
of the secondary table that it makes while compiling a relationship join, which the rewrite filters with
``relationship.and_()``; SQLAlchemy adapts those criteria to the alias from 2.0.28 on."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from sqlalchemy_easy_softdelete.handler.rewriter import SECONDARY_CRITERIA_SUPPORTED
from tests.secondary_tables.model import STParent, STTag

needs_secondary_criteria = pytest.mark.skipif(
    not SECONDARY_CRITERIA_SUPPORTED,
    reason="SQLAlchemy before 2.0.28 does not adapt relationship.and_() criteria to the secondary table's alias",
)


@needs_secondary_criteria
@pytest.mark.parametrize("form", ["relationship", "target_and_relationship"])
def test_join_along_a_relationship_with_a_soft_deletable_secondary_table(db_session, form):
    stmt = select(STParent.id, STTag.id)
    stmt = stmt.join(STParent.tags) if form == "relationship" else stmt.join(STTag, STParent.tags)

    for _ in range(2):
        assert db_session.execute(stmt).all() == [(1, 10)]


@needs_secondary_criteria
def test_outer_join_along_a_relationship_with_a_soft_deletable_secondary_table(db_session):
    rows = db_session.execute(select(STParent.id, STTag.id).outerjoin(STParent.tags)).all()

    assert rows == [(1, 10)]


@needs_secondary_criteria
def test_selectin_load_through_a_soft_deletable_secondary_table(db_session):
    parent = db_session.scalars(select(STParent).options(selectinload(STParent.tags))).one()

    assert [tag.id for tag in parent.tags] == [10]


def test_lazy_load_through_a_soft_deletable_secondary_table(db_session):
    parent = db_session.get(STParent, 1)

    assert [tag.id for tag in parent.tags] == [10]


def test_include_deleted_on_a_join_with_a_soft_deletable_secondary_table(db_session):
    stmt = select(STParent.id, STTag.id).join(STParent.tags).order_by(STTag.id).execution_options(include_deleted=True)

    assert db_session.execute(stmt).all() == [(1, 10), (1, 20)]
