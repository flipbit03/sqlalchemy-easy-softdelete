"""Instances loaded through rewritten statements can be pickled.

Instances keep the loader criteria of the query that loaded them, for their lazy loads, so pickling
an instance pickles those criteria too.
"""

import pickle
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import aliased, joinedload, selectinload

from tests.default_config.model import SDParent

LOADS: dict[str, Callable[[], Any]] = {
    "plain": lambda: select(SDParent).where(SDParent.id == 1000),
    "joinedload": lambda: select(SDParent).where(SDParent.id == 1000).options(joinedload(SDParent.children)),
    "selectinload": lambda: select(SDParent).where(SDParent.id == 1000).options(selectinload(SDParent.children)),
    "aliased_subquery": lambda: (lambda alias: select(alias).where(alias.id == 1000))(
        aliased(SDParent, select(SDParent).subquery())
    ),
}


@pytest.mark.parametrize("make", LOADS.values(), ids=LOADS.keys())
def test_loaded_instance_can_be_pickled(seeded_session, make):
    parent = seeded_session.scalars(make()).unique().one()
    children = sorted(child.id for child in parent.children)

    restored = pickle.loads(pickle.dumps(parent))

    assert restored.id == parent.id
    assert sorted(child.id for child in restored.children) == children
