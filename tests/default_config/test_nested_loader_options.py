"""Eager loading specified with ``Load.options()`` filters the entities of every path it loads."""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Load, defaultload, joinedload, selectinload

from tests.default_config.model import SDChild, SDParent

NESTED_LOADER_OPTIONS: dict[str, Callable[[], Any]] = {
    "defaultload_options_joinedload": lambda: defaultload(SDParent.children).options(
        joinedload(SDChild.child_children)
    ),
    "joinedload_options_joinedload": lambda: joinedload(SDParent.children).options(joinedload(SDChild.child_children)),
    "selectinload_options_joinedload": lambda: selectinload(SDParent.children).options(
        joinedload(SDChild.child_children)
    ),
    "load_options_joinedload_chain": lambda: Load(SDParent).options(
        joinedload(SDParent.children).joinedload(SDChild.child_children)
    ),
}


@pytest.mark.parametrize("make_option", NESTED_LOADER_OPTIONS.values(), ids=NESTED_LOADER_OPTIONS.keys())
def test_nested_loader_options_filter_the_entities_they_load(seeded_session, make_option):
    parents = seeded_session.scalars(select(SDParent).options(make_option())).unique().all()

    children = [child for parent in parents for child in parent.children]
    grandchildren = [grandchild for child in children for grandchild in child.child_children]
    assert children
    assert grandchildren
    assert all(child.deleted_at is None for child in children)
    assert all(grandchild.deleted_at is None for grandchild in grandchildren)
