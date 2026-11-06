"""Joined eager loading through a soft-deletable secondary table or subquery leaves out the soft-deleted links.
SQLAlchemy joins its own alias of the secondary for it, which the statement does not hold; the rewrite adds the
eager load again with the secondary's condition in ``relationship.and_()``, which SQLAlchemy adapts to that alias."""

import pytest
from sqlalchemy import exc, select
from sqlalchemy.orm import (
    aliased,
    contains_eager,
    defaultload,
    joinedload,
    lazyload,
    raiseload,
    selectinload,
    subqueryload,
    with_polymorphic,
)

from tests.joined_secondaries.model import JSEagerParent, JSItem, JSOwner, JSParent, JSSpecialItem, JSTag

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


@pytest.mark.parametrize("entity", [JSParent, JSEagerParent], ids=["relationship", "lazy_joined_relationship"])
def test_contains_eager_through_a_soft_deletable_secondary(db_session, entity):
    """contains_eager() loads from the statement's own join, which the rewrite filters (#65)."""
    stmt = select(entity).join(entity.tags).options(contains_eager(entity.tags))

    assert _tag_ids(db_session.scalars(stmt).unique().one()) == [10]


@pytest.mark.parametrize(
    "make_option",
    [lazyload, selectinload, subqueryload, joinedload],
    ids=["lazyload", "selectinload", "subqueryload", "joinedload"],
)
def test_option_on_a_lazy_joined_relationship_through_a_soft_deletable_secondary(db_session, make_option):
    """The statement's own option for the relationship keeps its strategy."""
    parent = db_session.scalars(select(JSEagerParent).options(make_option(JSEagerParent.tags))).unique().one()

    assert _tag_ids(parent) == [10]


def test_option_with_criteria_on_a_lazy_joined_relationship_through_a_soft_deletable_secondary(db_session):
    stmt = select(JSEagerParent).options(joinedload(JSEagerParent.tags.and_(JSTag.id > 10), innerjoin=True))

    assert db_session.scalars(stmt).unique().all() == []


def test_raiseload_wildcard_keeps_a_lazy_joined_relationship_through_a_secondary_unloaded(db_session):
    parent = db_session.scalars(select(JSEagerParent).options(raiseload("*"))).unique().one()

    with pytest.raises(exc.InvalidRequestError):
        _tag_ids(parent)


def test_defaultload_with_column_options_keeps_a_lazy_joined_relationship_filtered(db_session):
    stmt = select(JSEagerParent).options(defaultload(JSEagerParent.tags).load_only(JSTag.id))

    assert _tag_ids(db_session.scalars(stmt).unique().one()) == [10]


def test_two_joinedload_options_for_the_same_path(db_session):
    """The later option wins, as in SQLAlchemy, with its criteria."""
    stmt = select(JSParent).options(joinedload(JSParent.tags), joinedload(JSParent.tags.and_(JSTag.id > 10)))

    assert _tag_ids(db_session.scalars(stmt).unique().one()) == []


def test_joinedload_of_type_a_subclass_through_a_soft_deletable_secondary(db_session):
    stmt = select(JSOwner).options(joinedload(JSOwner.items.of_type(JSSpecialItem)).joinedload(JSSpecialItem.tags))

    assert _tag_ids(db_session.scalars(stmt).unique().one().items[0]) == [10]


def test_joinedload_through_a_with_polymorphic_entity_runs(db_session):
    """A path through a with_polymorphic() entity cannot be named again, so it is left as it is."""
    items = with_polymorphic(JSItem, [JSSpecialItem], aliased=True)
    stmt = select(JSOwner).options(joinedload(JSOwner.items.of_type(items)).joinedload(items.JSSpecialItem.tags))

    assert 10 in _tag_ids(db_session.scalars(stmt).unique().one().items[0])
