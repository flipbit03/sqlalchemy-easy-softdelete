"""Eager loading a relationship as a subclass of its target (``of_type()``) leaves out soft-deleted rows, for
single-table and joined-table inheritance. SQLAlchemy applies loader criteria by the relationship's target."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import aliased, joinedload, selectinload, subqueryload

from tests.of_type_subclasses.model import OTParent, OTSpecialChild, OTSpecialItem

loaders = pytest.mark.parametrize(
    "loader", [joinedload, selectinload, subqueryload], ids=lambda loader: loader.__name__
)

HIERARCHIES = {
    "single_table": ("children", OTSpecialChild),
    "joined_table": ("items", OTSpecialItem),
}
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
        assert _loaded_ids(db_session, option, attribute_name) == [1, 3]


@loaders
@hierarchies
def test_eager_load_of_type_an_alias_of_a_subclass(db_session, loader, hierarchy):
    """An alias of the subclass loads the subclass's rows only."""
    attribute_name, subclass = hierarchy
    option = loader(getattr(OTParent, attribute_name).of_type(aliased(subclass)))

    assert _loaded_ids(db_session, option, attribute_name) == [3]


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

    assert [row.id for row in getattr(parent, attribute_name)] == [1, 2, 3, 4]
