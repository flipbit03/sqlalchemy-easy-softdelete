"""SQLAlchemy passes a statement's loader options on to each relationship load it runs, those of other paths
too. A loader option into an ``aliased()`` subquery that cannot be filtered (one without the soft-delete
column, or one that includes soft-deleted rows) concerns its own path only: a sibling load into an alias of
the same class is still filtered."""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import aliased, defaultload, lazyload, selectinload, subqueryload

from tests.sibling_loader_paths.model import SLChild, SLParent


def _partial() -> Any:
    return aliased(SLChild, select(SLChild.id, SLChild.parent_id).subquery())


def _with_deleted() -> Any:
    return aliased(SLChild, select(SLChild).execution_options(include_deleted=True).subquery())


OPTIONS_INTO_AN_UNFILTERABLE_ALIAS: dict[str, Callable[[], Any]] = {
    "lazyload_partial": lambda: lazyload(SLParent.children.of_type(_partial())),
    "defaultload_partial": lambda: defaultload(SLParent.children.of_type(_partial())),
    "lazyload_include_deleted": lambda: lazyload(SLParent.children.of_type(_with_deleted())),
}


@pytest.mark.parametrize("loader", [selectinload, subqueryload], ids=lambda loader: loader.__name__)
@pytest.mark.parametrize(
    "make_option", OPTIONS_INTO_AN_UNFILTERABLE_ALIAS.values(), ids=OPTIONS_INTO_AN_UNFILTERABLE_ALIAS.keys()
)
def test_sibling_load_into_an_alias_is_filtered(db_session, make_option, loader):
    stmt = select(SLParent).options(make_option(), loader(SLParent.kids.of_type(aliased(SLChild))))

    for _ in range(2):
        db_session.expunge_all()
        parent = db_session.scalars(stmt).one()

        assert [kid.id for kid in parent.kids] == [1]
