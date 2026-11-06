"""SELECTs nested in a statement, and in the column_property() expressions of the classes it loads, leave out
soft-deleted rows: EXISTS, IN and scalar subqueries, and the relationship comparisons any() and has() that build
them. One that sets include_deleted keeps them."""

from typing import Any

import pytest
from sqlalchemy import exists, func, lambda_stmt, select
from sqlalchemy.orm import aliased, joinedload, undefer

from tests.nested_selects.model import NSChild, NSOwner, NSParent

child_table = NSChild.__table__


def _ids(session, stmt):  # type: ignore[no-untyped-def]
    return sorted(session.scalars(stmt))


def _children_exist(include_deleted: bool = False) -> Any:
    children = select(NSChild.id).where(NSChild.parent_id == NSParent.id)
    return exists(children.execution_options(include_deleted=True) if include_deleted else children)


def _children_count(include_deleted: bool = False) -> Any:
    count = select(func.count(NSChild.id)).where(NSChild.parent_id == NSParent.id)
    return (count.execution_options(include_deleted=True) if include_deleted else count).scalar_subquery()


WHERE_CLAUSES = {
    "any": lambda: NSParent.children.any(),
    "exists": lambda: _children_exist(),
    "in": lambda: NSParent.id.in_(select(NSChild.parent_id)),
    "core_exists": lambda: exists(select(child_table.c.id).where(child_table.c.parent_id == NSParent.id)),
    "scalar": lambda: _children_count() >= 1,
}


@pytest.mark.parametrize("make_clause", WHERE_CLAUSES.values(), ids=WHERE_CLAUSES.keys())
def test_select_nested_in_the_where_clause(db_session, make_clause):
    assert _ids(db_session, select(NSParent.id).where(make_clause())) == [1]


def test_negated_any(db_session):
    assert _ids(db_session, select(NSParent.id).where(~NSParent.children.any())) == [2, 3]


def test_has(db_session):
    """Child 4 is active, but its parent is soft-deleted."""
    assert _ids(db_session, select(NSChild.id).where(NSChild.parent.has())) == [1]


def test_include_deleted_on_a_select_nested_in_the_where_clause(db_session):
    """Statements of the same shape with and without include_deleted on the nested SELECT, taking turns."""
    for include_deleted in (True, False, True, False):
        rows = _ids(db_session, select(NSParent.id).where(_children_exist(include_deleted)))

        assert rows == ([1, 2] if include_deleted else [1])


def test_scalar_subquery_in_the_columns(db_session):
    label = _children_count().label("n")

    rows = db_session.execute(select(NSParent.id, label).order_by(NSParent.id)).all()

    assert [(row.id, row.n) for row in rows] == [(1, 1), (2, 0), (3, 0)]
    assert [row._mapping[label] for row in rows] == [1, 0, 0]


def test_include_deleted_on_a_scalar_subquery_in_the_columns(db_session):
    for include_deleted in (True, False, True, False):
        rows = db_session.execute(select(NSParent.id, _children_count(include_deleted)).order_by(NSParent.id)).all()

        assert rows == ([(1, 2), (2, 1), (3, 0)] if include_deleted else [(1, 1), (2, 0), (3, 0)])


def test_select_nested_in_the_having_clause(db_session):
    stmt = (
        select(NSParent.id)
        .outerjoin(NSParent.children)
        .group_by(NSParent.id)
        .having(exists(select(NSChild.id).where(NSChild.parent_id == NSParent.id).correlate(NSParent)))
    )

    assert _ids(db_session, stmt) == [1]


def test_select_nested_in_the_order_by_clause(db_session):
    stmt = select(NSParent.id).order_by(_children_count().desc(), NSParent.id.desc())

    assert list(db_session.scalars(stmt)) == [1, 3, 2]


def test_select_nested_in_a_join_on_clause(db_session):
    stmt = select(NSOwner.id).join(NSParent, (NSOwner.parent_id == NSParent.id) & _children_exist())

    assert _ids(db_session, stmt) == [1]


def test_select_correlated_to_an_aliased_entity(db_session):
    parent = aliased(NSParent)
    stmt = select(parent.id).where(exists(select(NSChild.id).where(NSChild.parent_id == parent.id)))

    assert _ids(db_session, stmt) == [1]


def test_select_correlated_to_a_subquery(db_session):
    parents = select(NSParent).subquery()
    stmt = select(parents.c.id).where(exists(select(NSChild.id).where(NSChild.parent_id == parents.c.id)))

    assert _ids(db_session, stmt) == [1]


def test_select_nested_in_a_nested_select(db_session):
    """The innermost SELECT correlates to the one around it, and filters its own table."""
    innermost = exists(select(NSParent.id).where(NSParent.id == NSChild.parent_id))
    stmt = select(NSParent.id).where(exists(select(NSChild.id).where(NSChild.parent_id == NSParent.id, innermost)))

    assert _ids(db_session, stmt) == [1]


def test_lambda_statement_with_a_nested_select(db_session):
    for _ in range(2):
        assert _ids(db_session, lambda_stmt(lambda: select(NSParent.id).where(NSParent.children.any()))) == [1]


def test_include_deleted_on_the_statement(db_session):
    stmt = select(NSParent.id).where(NSParent.children.any()).execution_options(include_deleted=True)

    assert _ids(db_session, stmt) == [1, 2, 4]


def test_column_property(db_session):
    parents = db_session.scalars(select(NSParent).order_by(NSParent.id))

    assert [(parent.id, parent.child_count) for parent in parents] == [(1, 1), (2, 0), (3, 0)]


def test_column_property_that_includes_deleted_rows(db_session):
    parents = db_session.scalars(select(NSParent).order_by(NSParent.id))

    assert [(parent.id, parent.every_child_count) for parent in parents] == [(1, 2), (2, 1), (3, 0)]


def test_column_property_selected_as_a_column(db_session):
    assert list(db_session.scalars(select(NSParent.child_count).order_by(NSParent.id))) == [1, 0, 0]


@pytest.mark.parametrize("undeferred", [False, True], ids=["deferred", "undefer"])
def test_deferred_column_property(db_session, undeferred):
    stmt = select(NSParent).order_by(NSParent.id)
    stmt = stmt.options(undefer(NSParent.deferred_child_count)) if undeferred else stmt

    assert [parent.deferred_child_count for parent in db_session.scalars(stmt)] == [1, 0, 0]


def test_column_property_of_an_eager_or_lazy_loaded_class(db_session):
    owner = (
        db_session.scalars(select(NSOwner).where(NSOwner.id == 1).options(joinedload(NSOwner.parent))).unique().one()
    )
    assert owner.parent.child_count == 1

    db_session.expunge_all()
    assert db_session.get(NSOwner, 1).parent.child_count == 1
