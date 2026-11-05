"""Joined eager loading through a soft-deletable secondary table or subquery leaves out the soft-deleted links.
SQLAlchemy joins its own alias of the secondary for it, which the statement does not hold; the rewrite adds the
eager load again with the secondary's condition in ``relationship.and_()``, which SQLAlchemy adapts to that alias."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import aliased, joinedload, selectinload

from tests.joined_secondaries.model import JSEagerParent, JSOwner, JSParent, JSTag

RELATIONSHIPS = pytest.mark.parametrize("attribute_name", ["tags", "tags_by_rows"])


def _tag_ids(parent, attribute_name="tags"):  # type: ignore[no-untyped-def]
    return [tag.id for tag in getattr(parent, attribute_name)]


@RELATIONSHIPS
def test_joinedload_through_a_soft_deletable_secondary(db_session, attribute_name):
    stmt = select(JSParent).options(joinedload(getattr(JSParent, attribute_name)))

    for _ in range(2):
        db_session.expunge_all()
        assert _tag_ids(db_session.scalars(stmt).unique().one(), attribute_name) == [10]


def test_joinedload_with_an_inner_join(db_session):
    parent = db_session.scalars(select(JSParent).options(joinedload(JSParent.tags, innerjoin=True))).unique().one()

    assert _tag_ids(parent) == [10]


def test_joinedload_keeps_the_options_own_criteria(db_session):
    stmt = select(JSParent).options(joinedload(JSParent.tags.and_(JSTag.id > 10)))

    assert _tag_ids(db_session.scalars(stmt).unique().one()) == []


def test_joinedload_of_type_an_alias_of_the_target(db_session):
    stmt = select(JSParent).options(joinedload(JSParent.tags.of_type(aliased(JSTag))))

    assert _tag_ids(db_session.scalars(stmt).unique().one()) == [10]


def test_joinedload_from_an_aliased_entity(db_session):
    parent = aliased(JSParent)

    assert _tag_ids(db_session.scalars(select(parent).options(joinedload(parent.tags))).unique().one()) == [10]


@pytest.mark.parametrize("loader", [joinedload, selectinload], ids=lambda loader: loader.__name__)
def test_joinedload_nested_under_another_load(db_session, loader):
    stmt = select(JSOwner).options(loader(JSOwner.parents).joinedload(JSParent.tags))

    assert _tag_ids(db_session.scalars(stmt).unique().one().parents[0]) == [10]


def test_lazy_joined_relationship_through_a_soft_deletable_secondary(db_session):
    assert _tag_ids(db_session.scalars(select(JSEagerParent)).unique().one()) == [10]


def test_lazy_joined_relationship_reached_by_a_joinedload(db_session):
    owner = db_session.scalars(select(JSOwner).options(joinedload(JSOwner.eager_parents))).unique().one()

    assert _tag_ids(owner.eager_parents[0]) == [10]


def test_joinedload_through_a_secondary_with_include_deleted(db_session):
    stmt = select(JSParent).options(joinedload(JSParent.tags)).execution_options(include_deleted=True)

    assert _tag_ids(db_session.scalars(stmt).unique().one()) == [10, 20]
