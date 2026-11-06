"""SELECTs nested in a statement, and in the column_property() expressions of the classes it loads, leave out
soft-deleted rows: EXISTS, IN and scalar subqueries, and the relationship comparisons any() and has() that build
them. One that sets include_deleted keeps them."""

from typing import Any

import pytest
from sqlalchemy import exists, func, lambda_stmt, select, union
from sqlalchemy.orm import aliased, joinedload, selectinload, undefer

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
    for include_deleted in (True, False, True, False):
        stmt = select(NSParent.id).where(NSParent.children.any()).execution_options(include_deleted=include_deleted)

        assert _ids(db_session, stmt) == ([1, 2, 4] if include_deleted else [1])


def test_column_property(db_session):
    parents = db_session.scalars(select(NSParent).order_by(NSParent.id))

    assert [(parent.id, parent.child_count) for parent in parents] == [(1, 1), (2, 0), (3, 0)]


def test_column_property_that_includes_deleted_rows(db_session):
    parents = db_session.scalars(select(NSParent).order_by(NSParent.id))

    rows = [(parent.id, parent.child_count, parent.every_child_count) for parent in parents]
    assert rows == [(1, 1, 2), (2, 0, 1), (3, 0, 0)]


def test_column_property_selected_as_a_column(db_session):
    assert list(db_session.scalars(select(NSParent.child_count).order_by(NSParent.id))) == [1, 0, 0]


@pytest.mark.parametrize("undeferred", [False, True], ids=["deferred", "undefer"])
def test_deferred_column_property(db_session, undeferred):
    stmt = select(NSParent).order_by(NSParent.id)
    stmt = stmt.options(undefer(NSParent.deferred_child_count)) if undeferred else stmt

    assert [parent.deferred_child_count for parent in db_session.scalars(stmt)] == [1, 0, 0]


def test_column_property_of_an_eager_or_lazy_loaded_class(db_session):
    """Including the column_property() that sets include_deleted."""
    for loader in (joinedload, selectinload):
        owner = (
            db_session.scalars(select(NSOwner).where(NSOwner.id == 1).options(loader(NSOwner.parent))).unique().one()
        )
        assert (owner.parent.child_count, owner.parent.every_child_count) == (1, 2)
        db_session.expunge_all()

    parent = db_session.get(NSOwner, 1).parent
    assert (parent.child_count, parent.every_child_count) == (1, 2)


def test_select_nested_in_the_columns_and_the_group_by_clause(db_session):
    """PostgreSQL matches GROUP BY, and the ORDER BY of a DISTINCT statement, to the select list by expression."""
    count = _children_count()
    label = _children_count().label("n")

    by_count = select(count, func.count(NSParent.id)).group_by(count).order_by(count)
    by_label = select(label, func.count(NSParent.id)).group_by(label).order_by(label)
    distinct = select(NSParent.id, count).distinct().order_by(count, NSParent.id)

    assert db_session.execute(by_count).all() == [(0, 2), (1, 1)]
    assert db_session.execute(by_label).all() == [(0, 2), (1, 1)]
    assert db_session.execute(distinct).all() == [(2, 0), (3, 0), (1, 1)]


def test_column_property_in_the_group_by_and_order_by_clauses(db_session):
    grouped = (
        select(NSParent.child_count, func.count(NSParent.id))
        .group_by(NSParent.child_count)
        .order_by(NSParent.child_count)
    )
    distinct = select(NSParent).distinct().order_by(NSParent.child_count, NSParent.id)

    assert db_session.execute(grouped).all() == [(0, 2), (1, 1)]
    assert [parent.id for parent in db_session.scalars(distinct)] == [2, 3, 1]


def test_include_deleted_on_a_scalar_subquery_next_to_a_column_property(db_session):
    rows = db_session.execute(select(NSParent, _children_count(include_deleted=True)).order_by(NSParent.id)).all()

    assert [(parent.id, parent.child_count, every) for parent, every in rows] == [(1, 1, 2), (2, 0, 1), (3, 0, 0)]


def test_include_deleted_on_a_select_nested_two_levels_deep(db_session):
    """Parents with an owner whose parent has children, with and without the soft-deleted ones, taking turns."""
    for include_deleted in (True, False, True, False):
        children = select(NSChild.id).where(NSChild.parent_id == NSOwner.parent_id)
        children = children.execution_options(include_deleted=True) if include_deleted else children
        owners = select(NSOwner.id).where(NSOwner.parent_id == NSParent.id, exists(children))
        owned = select(NSOwner.parent_id).where(exists(children)).subquery()

        in_where = select(NSParent).where(exists(owners)).order_by(NSParent.id)
        in_from = select(NSParent).join(owned, owned.c.parent_id == NSParent.id).order_by(NSParent.id)

        expected = [1, 2] if include_deleted else [1]
        assert [parent.id for parent in db_session.scalars(in_where)] == expected
        assert [parent.id for parent in db_session.scalars(in_from)] == expected


def test_uncorrelated_select_of_the_same_class(db_session):
    """A nested SELECT with a single FROM does not correlate, even to a FROM of the statement around it."""
    latest_child = select(func.max(NSChild.id)).group_by(NSChild.parent_id)
    last_parent = select(func.max(NSParent.id)).scalar_subquery()

    assert _ids(db_session, select(NSChild.id).where(NSChild.id.in_(latest_child))) == [1, 4]
    assert _ids(db_session, select(NSParent.id).where(NSParent.id == last_parent)) == [3]


def test_include_deleted_on_an_uncorrelated_select_of_the_same_class(db_session):
    """Statements of the same shape with and without include_deleted on the nested SELECT, taking turns."""
    for include_deleted in (True, False, True, False):
        count = select(func.count(NSParent.id))
        count = count.execution_options(include_deleted=True) if include_deleted else count
        stmt = select(NSParent).where(NSParent.id + count.scalar_subquery() == 6)

        assert [parent.id for parent in db_session.scalars(stmt)] == ([2] if include_deleted else [3])


def test_select_with_a_join_nested_in_a_nested_select(db_session):
    innermost = exists(select(NSParent.id).where(NSParent.id == NSChild.parent_id))
    middle = (
        select(NSChild.id)
        .outerjoin(NSOwner, NSOwner.parent_id == NSChild.parent_id)
        .where(NSChild.parent_id == NSParent.id, innermost)
    )

    assert _ids(db_session, select(NSParent.id).where(exists(middle))) == [1]


def test_union_nested_in_the_where_clause(db_session):
    """Each SELECT of the union filters its own table. Parent 2's only child is soft-deleted, owner 1's parent is 1."""
    children = select(NSChild.parent_id).where(NSChild.parent_id == 2)
    owners = select(NSOwner.parent_id).where(NSOwner.id == 1)

    assert _ids(db_session, select(NSParent.id).where(NSParent.id.in_(union(children, owners)))) == [1]
    assert _ids(db_session, select(NSParent.id).where(NSParent.id.in_(union(owners, children)))) == [1]


def test_select_nested_in_the_group_by_and_having_clauses(db_session):
    """PostgreSQL matches HAVING to GROUP BY by expression."""
    count = _children_count()
    by_count = select(count, func.count(NSParent.id)).group_by(count).having(count > 0)
    by_column_property = (
        select(NSParent.child_count, func.count(NSParent.id))
        .group_by(NSParent.child_count)
        .having(NSParent.child_count > 0)
    )

    assert db_session.execute(by_count).all() == [(1, 1)]
    assert db_session.execute(by_column_property).all() == [(1, 1)]


def test_select_nested_in_a_nested_select_that_correlates_away_the_table(db_session):
    """The innermost SELECT does not correlate to the parents of the statement two levels up, which the one
    around it correlates to: it selects from them again. Child 4 is active, but its parent is soft-deleted."""
    for child_id, expected in ((4, []), (1, [1, 2])):
        innermost = exists(select(NSChild.id).where(NSChild.parent_id == NSParent.id, NSChild.id == child_id))
        owners = select(NSOwner.id).where(NSOwner.parent_id == NSParent.id, innermost)

        assert _ids(db_session, select(NSParent.id).where(exists(owners))) == expected


@pytest.mark.parametrize(
    "make_clause", [WHERE_CLAUSES["any"], WHERE_CLAUSES["core_exists"]], ids=["any", "core_exists"]
)
def test_select_nested_in_the_having_clause_only(db_session, make_clause):
    """One that is not also in the columns or GROUP BY clause is filtered like one of the WHERE clause."""
    assert _ids(db_session, select(NSParent.id).group_by(NSParent.id).having(make_clause())) == [1]


def test_select_nested_in_the_criterion_of_any_or_has(db_session):
    """Owners whose parent has an active child: owner 2's parent only has a soft-deleted one."""
    has_any = select(NSOwner.id).where(NSOwner.parent.has(NSParent.children.any()))
    has_in = select(NSOwner.id).where(NSOwner.parent.has(NSParent.id.in_(select(NSChild.parent_id))))

    assert _ids(db_session, has_any) == [1]
    assert _ids(db_session, has_in) == [1]


def test_any_in_the_having_clause_next_to_a_scalar_subquery_in_the_columns(db_session):
    stmt = select(NSParent.id, _children_count().label("n")).group_by(NSParent.id).having(NSParent.children.any())

    assert db_session.execute(stmt).all() == [(1, 1)]


def test_select_nested_in_the_group_by_and_having_clauses_holding_a_select(db_session):
    """PostgreSQL matches HAVING to GROUP BY by expression, including the SELECT nested in it."""
    owned = exists(select(NSOwner.id).where(NSOwner.parent_id == NSChild.parent_id))
    count = select(func.count(NSChild.id)).where(NSChild.parent_id == NSParent.id, owned).scalar_subquery()
    stmt = (
        select(count, func.count(NSParent.id))
        .where(NSParent.id.in_(select(NSOwner.parent_id)))
        .group_by(count)
        .having(count >= 0)
        .order_by(count)
    )

    assert db_session.execute(stmt).all() == [(0, 1), (1, 1)]
