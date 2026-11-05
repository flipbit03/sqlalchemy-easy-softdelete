"""Soft-deleted rows of a relationship's secondary table do not link related rows. SQLAlchemy joins an alias
of the secondary table that it makes while compiling a relationship join, which the rewrite filters with
``relationship.and_()``; SQLAlchemy adapts those criteria to the alias from 2.0.28 on."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import aliased, selectinload

from tests.secondary_tables.model import STLink, STParent, STTag


@pytest.mark.parametrize("form", ["relationship", "target_and_relationship"])
def test_join_along_a_relationship_with_a_soft_deletable_secondary_table(db_session, form):
    stmt = select(STParent.id, STTag.id)
    stmt = stmt.join(STParent.tags) if form == "relationship" else stmt.join(STTag, STParent.tags)

    for _ in range(2):
        assert db_session.execute(stmt).all() == [(1, 10)]


def test_outer_join_along_a_relationship_with_a_soft_deletable_secondary_table(db_session):
    rows = db_session.execute(select(STParent.id, STTag.id).outerjoin(STParent.tags)).all()

    assert rows == [(1, 10)]


def test_selectin_load_through_a_soft_deletable_secondary_table(db_session):
    parent = db_session.scalars(select(STParent).options(selectinload(STParent.tags))).one()

    assert [tag.id for tag in parent.tags] == [10]


def test_lazy_load_through_a_soft_deletable_secondary_table(db_session):
    parent = db_session.get(STParent, 1)

    assert [tag.id for tag in parent.tags] == [10]


def test_include_deleted_on_a_join_with_a_soft_deletable_secondary_table(db_session):
    stmt = select(STParent.id, STTag.id).join(STParent.tags).order_by(STTag.id).execution_options(include_deleted=True)

    assert db_session.execute(stmt).all() == [(1, 10), (1, 20)]


def test_join_set_aside_by_with_only_columns_with_a_soft_deletable_secondary_table(db_session):
    stmt = select(STParent).join(STParent.tags).with_only_columns(STParent.id, STTag.id)

    assert db_session.execute(stmt).all() == [(1, 10)]
    assert db_session.query(STParent).join(STParent.tags).with_entities(STParent.id, STTag.id).all() == [(1, 10)]


def test_selectin_load_for_parents_loaded_with_include_deleted(db_session):
    """A selectin load selects its parents by primary key, from an alias of the parent table that SQLAlchemy
    makes: it loads the related rows of the soft-deleted parents a statement loaded on purpose."""
    db_session.add(STParent(id=2, deleted_at=datetime.now(timezone.utc)))
    db_session.flush()
    db_session.add(STLink(parent_id=2, tag_id=10))
    db_session.flush()
    db_session.expunge_all()
    everyone = aliased(STParent, select(STParent).execution_options(include_deleted=True).subquery())

    parents = db_session.scalars(select(everyone).options(selectinload(everyone.tags))).all()

    assert sorted((parent.id, [tag.id for tag in parent.tags]) for parent in parents) == [(1, [10]), (2, [10])]
