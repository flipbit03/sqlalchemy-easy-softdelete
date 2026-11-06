"""Eager loading a relationship as a subclass of its target (``of_type()``) leaves out soft-deleted rows, for
single-table and joined-table inheritance. SQLAlchemy applies loader criteria by the relationship's target."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import aliased, joinedload, selectinload, subqueryload

from tests.of_type_subclasses.model import OTLeafChild, OTParent, OTSpecialChild, OTSpecialItem

loaders = pytest.mark.parametrize(
    "loader", [joinedload, selectinload, subqueryload], ids=lambda loader: loader.__name__
)

HIERARCHIES = {
    "single_table": ("children", OTSpecialChild),
    "joined_table": ("items", OTSpecialItem),
}
# The live rows each load returns, of the base class and of the subclass and those below it.
ACTIVE = {OTSpecialChild: [1, 3, 5], OTSpecialItem: [1, 3]}
ACTIVE_OF_SUBCLASS = {OTSpecialChild: [3, 5], OTSpecialItem: [3]}
DELETED = {2, 4, 6}
hierarchies = pytest.mark.parametrize("hierarchy", HIERARCHIES.values(), ids=HIERARCHIES.keys())


def _loaded_ids(session, option, attribute_name):  # type: ignore[no-untyped-def]
    parent = session.scalars(select(OTParent).options(option)).unique().one()
    return [row.id for row in getattr(parent, attribute_name)]


@loaders
@hierarchies
def test_eager_load_of_type_a_subclass(db_session, loader, hierarchy):
    attribute_name, subclass = hierarchy
    option = loader(getattr(OTParent, attribute_name).of_type(subclass))

    for _ in range(2):
        db_session.expunge_all()
        assert _loaded_ids(db_session, option, attribute_name) == ACTIVE[subclass]


@loaders
@hierarchies
def test_eager_load_of_type_an_alias_of_a_subclass(db_session, loader, hierarchy):
    """An alias of the subclass loads the subclass's rows only."""
    attribute_name, subclass = hierarchy
    option = loader(getattr(OTParent, attribute_name).of_type(aliased(subclass)))

    assert _loaded_ids(db_session, option, attribute_name) == ACTIVE_OF_SUBCLASS[subclass]


@loaders
@hierarchies
def test_eager_load_of_type_a_subclass_with_include_deleted(db_session, loader, hierarchy):
    attribute_name, subclass = hierarchy
    stmt = (
        select(OTParent)
        .options(loader(getattr(OTParent, attribute_name).of_type(subclass)))
        .execution_options(include_deleted=True)
    )

    parent = db_session.scalars(stmt).unique().one()

    ids = [row.id for row in getattr(parent, attribute_name)]
    assert set(ids) & DELETED
    assert [row_id for row_id in ids if row_id not in DELETED] == ACTIVE[subclass]


@pytest.mark.parametrize("loader", [selectinload, subqueryload], ids=lambda loader: loader.__name__)
@hierarchies
def test_eager_load_of_type_an_alias_of_a_subclass_over_a_select_without_the_column(db_session, loader, hierarchy):
    """The eager loading criteria do not reach an alias over a SELECT that leaves out the soft-delete column, which
    it filters itself."""
    attribute_name, subclass = hierarchy
    columns = [subclass.id, subclass.parent_id, subclass.kind]
    option = loader(getattr(OTParent, attribute_name).of_type(aliased(subclass, select(*columns).subquery())))

    assert _loaded_ids(db_session, option, attribute_name) == ACTIVE_OF_SUBCLASS[subclass]


@pytest.mark.parametrize("loader", [joinedload, selectinload], ids=lambda loader: loader.__name__)
@hierarchies
def test_include_deleted_alias_of_a_subclass_next_to_an_eager_load_of_type_it(db_session, loader, hierarchy):
    """An alias of the subclass over a SELECT that includes soft-deleted rows keeps them, next to the criteria for
    the hierarchy's base class."""
    attribute_name, subclass = hierarchy
    every_row = aliased(subclass, select(subclass).execution_options(include_deleted=True).subquery())
    stmt = (
        select(OTParent, every_row.id)
        .join(every_row, every_row.parent_id == OTParent.id)
        .options(loader(getattr(OTParent, attribute_name).of_type(subclass)))
    )

    rows = db_session.execute(stmt).unique().all()

    assert sorted(row_id for _, row_id in rows) == sorted(
        ACTIVE_OF_SUBCLASS[subclass] + [4] + ([6] if subclass is OTSpecialChild else [])
    )
    assert [row.id for row in getattr(rows[0][0], attribute_name)] == ACTIVE[subclass]


@pytest.mark.parametrize("loader", [selectinload, subqueryload], ids=lambda loader: loader.__name__)
def test_eager_load_of_a_relationship_to_a_mid_level_class_of_type_a_subclass(db_session, loader):
    """SQLAlchemy applies criteria by the hierarchy's base class, also when the relationship targets a class below it."""
    option = loader(OTParent.special_children.of_type(OTLeafChild))
    every_row = (
        db_session.scalars(select(OTParent).options(option).execution_options(include_deleted=True)).unique().one()
    )
    every_id = [row.id for row in every_row.special_children]
    db_session.expunge_all()

    ids = _loaded_ids(db_session, option, "special_children")

    assert ids == [row_id for row_id in every_id if row_id not in DELETED]
    assert 5 in ids
