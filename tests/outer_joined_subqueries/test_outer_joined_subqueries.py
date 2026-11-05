"""A class mapped to a SELECT that outer joins a soft-deletable table: the table's soft-deleted rows leave NULLs, as
if they did not exist, and never show."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload, subqueryload

from tests.outer_joined_subqueries.model import OJChildNickname, OJParent

# Child 4's nickname is soft-deleted: it has none.
ACTIVE_ROWS = [(1, None), (3, 1), (4, None)]


def test_select_from_it(db_session):
    rows = [
        (row.id, row.nickname_id) for row in db_session.scalars(select(OJChildNickname).order_by(OJChildNickname.id))
    ]

    assert rows == ACTIVE_ROWS


def test_lazy_load_of_it(db_session):
    parent = db_session.get(OJParent, 1)

    assert [(row.id, row.nickname_id) for row in parent.child_nicknames] == ACTIVE_ROWS


@pytest.mark.parametrize("loader", [selectinload, subqueryload], ids=lambda loader: loader.__name__)
def test_eager_load_of_it(db_session, loader):
    parent = db_session.scalars(select(OJParent).options(loader(OJParent.child_nicknames))).one()

    assert [(row.id, row.nickname_id) for row in parent.child_nicknames] == ACTIVE_ROWS


def test_joined_eager_load_of_it_never_shows_a_soft_deleted_row(db_session):
    """A joined eager load selects SQLAlchemy's own alias of the SELECT, where a row's soft-deleted nickname would
    show: such a row is left out."""
    parent = db_session.scalars(select(OJParent).options(joinedload(OJParent.child_nicknames))).unique().one()

    assert [(row.id, row.nickname_id) for row in parent.child_nicknames] == [(1, None), (3, 1)]
