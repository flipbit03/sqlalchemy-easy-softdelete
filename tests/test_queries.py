"""Tests for `sqlalchemy_easy_softdelete` package."""

import pytest
from sqlalchemy import func, insert, lambda_stmt, select, table, text
from sqlalchemy.orm import Query, joinedload, selectinload, subqueryload
from sqlalchemy.orm.util import LoaderCriteriaOption
from sqlalchemy.sql import CompoundSelect, Select
from sqlalchemy.sql.lambdas import LambdaElement, LinkedLambdaElement, StatementLambdaElement

from tests.model import (
    SDBaseRequest,
    SDChild,
    SDChildChild,
    SDDerivedRequest,
    SDParent,
    SDSimpleTable,
    SDTableThatShouldNotBeSoftDeleted,
)
from tests.utils import is_filtering_for_softdeleted


def test_query_single_table(snapshot, seeded_session, rewriter):
    """Query with one table"""
    test_query: Query = seeded_session.query(SDChild)

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert (
        is_filtering_for_softdeleted(
            soft_deleted_rewritten_statement,
            {
                SDChild.__table__,
            },
        )
        is True
    )

    assert [repr(c) for c in sorted(test_query.all(), key=lambda i: i.id)] == snapshot


def test_query_with_join(snapshot, seeded_session, rewriter):
    """Query with a simple join"""
    test_query: Query = seeded_session.query(SDChild).join(SDParent)  # noqa -- wrong typing stub in SA

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert (
        is_filtering_for_softdeleted(soft_deleted_rewritten_statement, {SDChild.__table__, SDParent.__table__}) is True
    )

    assert [repr(c) for c in sorted(test_query.all(), key=lambda i: i.id)] == snapshot


def test_query_union_sdchild(snapshot, seeded_session, rewriter):
    """Two queries joined via UNION"""
    test_query: Query = seeded_session.query(SDChild).union(seeded_session.query(SDChild))

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert is_filtering_for_softdeleted(soft_deleted_rewritten_statement, {SDChild.__table__}) is True

    assert [repr(c) for c in sorted(test_query.all(), key=lambda i: i.id)] == snapshot


def test_query_union_sdchild_core(snapshot, seeded_session, rewriter):
    """Two queries joined via UNION, using SQLAlchemy Core"""
    sdchild = SDChild.__table__

    select_as_core = (select(sdchild.c.id, sdchild.c.parent_id).select_from(sdchild)).union(
        select(sdchild.c.id, sdchild.c.parent_id).select_from(sdchild)
    )

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(select_as_core)

    assert is_filtering_for_softdeleted(soft_deleted_rewritten_statement, {SDChild.__table__}) is True


def test_query_with_union_but_union_softdelete_disabled(snapshot, seeded_session, rewriter):
    """Two queries joined via UNION but the second one has soft-delete disabled"""

    # Two SDChild .all() queries with results joined via UNION
    # the first one has soft delete applied
    # the second one has soft delete DISABLED
    # the second query is a superset of the first one, and results in
    # all objects in the DB being returned
    test_query: Query = seeded_session.query(SDChild).union(
        seeded_session.query(SDChild).execution_options(include_deleted=True)
    )

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert is_filtering_for_softdeleted(soft_deleted_rewritten_statement, {SDChild.__table__}) is True

    all_children: list[SDChild] = seeded_session.query(SDChild).execution_options(include_deleted=True).all()

    assert sorted(test_query.all(), key=lambda x: x.id) == sorted(all_children, key=lambda x: x.id)

    assert [repr(c) for c in sorted(test_query.all(), key=lambda i: i.id)] == snapshot


def test_ensure_aggregate_from_multiple_table_deletion_works_active_object_count(snapshot, seeded_session, rewriter):
    """Aggregate function from a query that contains a join"""
    test_query: Query = seeded_session.query(SDChild).join(SDParent).with_entities(func.count())  # noqa

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert is_filtering_for_softdeleted(soft_deleted_rewritten_statement, {SDChild.__table__}) is True

    assert test_query.count() == snapshot


def test_ensure_table_with_inheritance_works(snapshot, seeded_session, rewriter):
    test_query: Query = seeded_session.query(SDDerivedRequest)

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert is_filtering_for_softdeleted(soft_deleted_rewritten_statement, {SDBaseRequest.__table__}) is True

    test_query_results = test_query.all()
    assert len(test_query_results) == 2
    assert [repr(r) for r in sorted(test_query_results, key=lambda i: i.id)] == snapshot

    all_active_and_deleted_derived_requests = (
        seeded_session.query(SDDerivedRequest).execution_options(include_deleted=True).all()
    )

    assert len(all_active_and_deleted_derived_requests) == 3
    assert [repr(r) for r in sorted(all_active_and_deleted_derived_requests, key=lambda i: i.id)] == snapshot


def test_ensure_table_with_inheritance_works_query_base(snapshot, seeded_session, rewriter):
    """
    Querying for a polymorphic entity *without JOIN* should work when fields contained in
    derived entities are lazily fetched.
    """

    # Query the BASE entity, without joins.
    test_query: Query = seeded_session.query(SDBaseRequest).filter(SDBaseRequest.request_type == "sdderivedrequest")

    request: SDDerivedRequest = test_query.first()

    try:
        # Accessing a field in a SDDerived Request will trigger an additional query with
        # a `FromStatement` as the statement, instead of a normal Select
        _ = request.derived_field
    except Exception as exc:
        raise AssertionError(f"Exception was raised {exc}") from exc


def test_query_with_text_clause_as_table(snapshot, seeded_session, rewriter):
    """We cannot parse information from a literal text table name -- return unchanged"""

    # Table as a TextClause
    test_query_text_clause: Select = select(text("id")).select_from(text("sdderivedrequest"))
    # Strip trailing whitespace from SQL lines for consistent comparison
    sql = "\n".join(line.rstrip() for line in str(rewriter.rewrite_statement(test_query_text_clause)).splitlines())
    assert sql == snapshot


def test_query_with_table_clause_as_table(snapshot, seeded_session, rewriter):
    """We cannot parse information from a literal text table name -- return unchanged"""

    # Table as a TableClause
    test_query_table_clause: Select = select(text("id")).select_from(table("sdderivedrequest"))
    # Strip trailing whitespace from SQL lines for consistent comparison
    sql = "\n".join(line.rstrip() for line in str(rewriter.rewrite_statement(test_query_table_clause)).splitlines())
    assert sql == snapshot


def test_insert_with_returning(snapshot, seeded_session, rewriter, db_connection):
    """Insert with RETURNING is considered a *Select* by SQLAlchemy, since it returns data :dizzy:
    that means we need to actively protect against this case"""

    # RETURNING is not supported in SQLite
    if db_connection.dialect.name == "sqlite":
        pytest.skip('SQLite does not support "INSERT...RETURNING"')

    insert_stmt = insert(SDSimpleTable).values(int_field=10).returning(SDSimpleTable)

    # Generate an Insert + RETURNING
    insert_returning = select(SDSimpleTable).from_statement(insert_stmt)

    result = seeded_session.execute(insert_returning)

    assert list(result)[0][0].int_field == 10


def test_query_with_more_than_one_join(snapshot, seeded_session, rewriter):
    test_query = (
        seeded_session
        .query(SDParent)
        .join(SDChild)
        .join(SDChildChild)
        .filter(
            SDParent.id > 0,
        )
    )

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert (
        is_filtering_for_softdeleted(
            soft_deleted_rewritten_statement,
            {
                SDParent.__table__,
                SDChild.__table__,
                SDChildChild.__table__,
            },
        )
        is True
    )


def test_query_with_same_field_as_softdelete_field_but_ignored(seeded_session, rewriter):
    """Test that a query with a field that has the same name as the soft-delete field
    but is ignored, does not get rewritten"""

    test_query = seeded_session.query(SDTableThatShouldNotBeSoftDeleted)

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert (
        is_filtering_for_softdeleted(
            soft_deleted_rewritten_statement,
            {
                SDTableThatShouldNotBeSoftDeleted.__table__,
            },
        )
        is False
    )


def test_query_with_joinedload_filters_soft_deleted(seeded_session, rewriter):
    """Test that joinedload properly filters soft-deleted related records.

    This is a regression test for soft deletion in joined subqueries.
    When using joinedload(), SQLAlchemy creates subqueries in the JOIN clause,
    and these subqueries must also have soft-delete filters applied.
    """
    # Query parent with eagerly loaded children using joinedload
    parent = seeded_session.query(SDParent).options(joinedload(SDParent.children)).filter(SDParent.id == 1000).first()

    # Parent should exist
    assert parent is not None
    assert parent.id == 1000

    # All loaded children should be non-deleted (deleted_at should be None)
    # This verifies that the soft-delete filter was applied to the joinedload subquery
    for child in parent.children:
        assert child.deleted_at is None, f"Child {child.id} should not be deleted but has deleted_at={child.deleted_at}"


def test_query_with_selectinload_filters_soft_deleted(seeded_session, rewriter):
    """Test that selectinload properly filters soft-deleted related records.

    selectinload uses a separate SELECT ... WHERE id IN (...) query to load related records.
    """
    parent = seeded_session.query(SDParent).options(selectinload(SDParent.children)).filter(SDParent.id == 1000).first()

    assert parent is not None
    assert parent.id == 1000

    # All loaded children should be non-deleted
    for child in parent.children:
        assert child.deleted_at is None, f"Child {child.id} should not be deleted but has deleted_at={child.deleted_at}"


def test_query_with_subqueryload_filters_soft_deleted(seeded_session, rewriter):
    """Test that subqueryload properly filters soft-deleted related records.

    subqueryload uses a subquery to load all related records in one query.
    """
    parent = seeded_session.query(SDParent).options(subqueryload(SDParent.children)).filter(SDParent.id == 1000).first()

    assert parent is not None
    assert parent.id == 1000

    # All loaded children should be non-deleted
    for child in parent.children:
        assert child.deleted_at is None, f"Child {child.id} should not be deleted but has deleted_at={child.deleted_at}"


def test_query_with_nested_joinedload_filters_soft_deleted(seeded_session, rewriter):
    """Test that chained/nested joinedload properly filters soft-deleted records at all levels.

    This tests: Parent -> Children -> Grandchildren with joinedload at each level.
    """
    parent = (
        seeded_session
        .query(SDParent)
        .options(joinedload(SDParent.children).joinedload(SDChild.child_children))
        .filter(SDParent.id == 1000)
        .first()
    )

    assert parent is not None
    assert parent.id == 1000

    # All children should be non-deleted
    for child in parent.children:
        assert child.deleted_at is None, f"Child {child.id} should not be deleted"

        # All grandchildren should also be non-deleted
        for grandchild in child.child_children:
            assert grandchild.deleted_at is None, (
                f"Grandchild {grandchild.id} should not be deleted but has deleted_at={grandchild.deleted_at}"
            )


def test_query_with_mixed_eager_loading_strategies(seeded_session, rewriter):
    """Test mixing different eager loading strategies in the same query.

    Uses joinedload for children and selectinload for grandchildren.
    """
    parent = (
        seeded_session
        .query(SDParent)
        .options(joinedload(SDParent.children).selectinload(SDChild.child_children))
        .filter(SDParent.id == 1000)
        .first()
    )

    assert parent is not None

    # All children should be non-deleted
    for child in parent.children:
        assert child.deleted_at is None, f"Child {child.id} should not be deleted"

        # All grandchildren should also be non-deleted
        for grandchild in child.child_children:
            assert grandchild.deleted_at is None, f"Grandchild {grandchild.id} should not be deleted"


def test_query_with_multiple_parents_and_eager_loading(seeded_session, rewriter):
    """Test eager loading across multiple parent records.

    Ensures soft-delete filtering works when loading children for multiple parents.
    """
    parents = (
        seeded_session
        .query(SDParent)
        .options(joinedload(SDParent.children))
        .filter(SDParent.id.in_([1000, 1001]))
        .all()
    )

    assert len(parents) == 2

    for parent in parents:
        # All children of each parent should be non-deleted
        for child in parent.children:
            assert child.deleted_at is None, f"Child {child.id} of parent {parent.id} should not be deleted"


def test_eager_loading_does_not_affect_normal_soft_delete_filtering(seeded_session, rewriter):
    """Verify that adding eager loading options doesn't break normal soft-delete filtering.

    The parent table itself should still be filtered for soft-deleted records.
    """
    # Parent 1002 is soft-deleted, so it should not appear in results
    parents = seeded_session.query(SDParent).options(joinedload(SDParent.children)).all()

    # Should only get parents 1000 and 1001, not 1002 (which is deleted)
    parent_ids = [p.id for p in parents]
    assert 1000 in parent_ids
    assert 1001 in parent_ids
    assert 1002 not in parent_ids, "Soft-deleted parent 1002 should not be in results"


# ==============================================================================
# Tests for lambda_stmt support
# ==============================================================================


def test_lambda_stmt_basic(seeded_session, rewriter):
    """Test that a basic lambda_stmt gets soft-delete filtering applied.

    This tests LambdaElement support.
    When using lambda_stmt (often used by advanced-alchemy), the soft-delete
    rewriter should handle the LambdaElement and apply filters to the underlying
    Select statement.
    """
    # Create a lambda_stmt query
    stmt = lambda_stmt(lambda: select(SDChild))

    # Verify it's a StatementLambdaElement
    assert isinstance(stmt, StatementLambdaElement)
    assert isinstance(stmt, LambdaElement)

    # Rewrite the statement
    rewritten = rewriter.rewrite_statement(stmt)

    # The rewritten statement should still be a LambdaElement
    assert isinstance(rewritten, LambdaElement)

    # Execute the query - it should only return non-deleted children
    result = seeded_session.execute(rewritten)
    children = result.scalars().all()

    # All children should have deleted_at = None
    for child in children:
        assert child.deleted_at is None, f"Child {child.id} should not be deleted but has deleted_at={child.deleted_at}"


def test_lambda_stmt_with_chained_operations(seeded_session, rewriter):
    """Test lambda_stmt with chained lambda operations (LinkedLambdaElement).

    When using lambda_stmt with additional chained lambdas (e.g. adding where clauses),
    SQLAlchemy creates LinkedLambdaElement objects. These should also be handled.
    """
    # Create a lambda_stmt with chained operations
    stmt = lambda_stmt(lambda: select(SDChild))
    stmt = stmt + (lambda s: s.where(SDChild.parent_id == 1000))

    # Verify it's a LinkedLambdaElement
    assert isinstance(stmt, LinkedLambdaElement)
    assert isinstance(stmt, LambdaElement)

    # Rewrite the statement
    rewritten = rewriter.rewrite_statement(stmt)

    # The rewritten statement should still be a LambdaElement
    assert isinstance(rewritten, LambdaElement)

    # Execute the query - it should only return non-deleted children of parent 1000
    result = seeded_session.execute(rewritten)
    children = result.scalars().all()

    # All children should have deleted_at = None and parent_id = 1000
    assert len(children) > 0, "Should have at least one child"
    for child in children:
        assert child.deleted_at is None, f"Child {child.id} should not be deleted"
        assert child.parent_id == 1000, f"Child {child.id} should have parent_id=1000"


def test_lambda_stmt_with_join(seeded_session, rewriter):
    """Test lambda_stmt with JOIN operations.

    Both tables in the join should have soft-delete filtering applied.
    """
    stmt = lambda_stmt(lambda: select(SDChild).join(SDParent))

    # Rewrite the statement
    rewritten = rewriter.rewrite_statement(stmt)

    # Execute - should only return non-deleted children of non-deleted parents
    result = seeded_session.execute(rewritten)
    children = result.scalars().all()

    # All children should be non-deleted
    for child in children:
        assert child.deleted_at is None, f"Child {child.id} should not be deleted"

    # Verify that children of deleted parent (1002) are not included
    child_parent_ids = {c.parent_id for c in children}
    assert 1002 not in child_parent_ids, "Children of soft-deleted parent 1002 should not appear"


def test_lambda_stmt_filters_applied_to_resolved(seeded_session, rewriter):
    """Verify that soft-delete filters are applied to the _resolved Select."""
    stmt = lambda_stmt(lambda: select(SDChild))

    # Rewrite
    rewritten = rewriter.rewrite_statement(stmt)

    # Get the resolved statement from the rewritten LambdaElement
    resolved_after = rewritten._resolved

    # The resolved statement should now have soft-delete filtering
    assert is_filtering_for_softdeleted(resolved_after, {SDChild.__table__}) is True


def test_lambda_stmt_integration_with_session_execute(seeded_session, rewriter):
    """Integration test: lambda_stmt should work when executed through session.

    This mimics the actual usage pattern from advanced-alchemy.
    """
    # Create a lambda_stmt for parents
    stmt = lambda_stmt(lambda: select(SDParent))

    # Execute directly through the session - the hook should intercept and rewrite
    result = seeded_session.execute(stmt)
    parents = result.scalars().all()

    # Should only get non-deleted parents (1000 and 1001, not 1002)
    parent_ids = [p.id for p in parents]
    assert 1000 in parent_ids
    assert 1001 in parent_ids
    assert 1002 not in parent_ids, "Soft-deleted parent 1002 should not be in results"


def test_lambda_stmt_with_union(seeded_session, rewriter):
    """Test lambda_stmt with UNION (CompoundSelect).

    When lambda_stmt wraps a UNION query, the _resolved attribute is a
    CompoundSelect. This should be properly rewritten.
    """
    # Create a lambda_stmt with UNION using the table directly for clearer results
    sdchild = SDChild.__table__
    stmt = lambda_stmt(
        lambda: (
            select(sdchild.c.id, sdchild.c.parent_id, sdchild.c.deleted_at)
            .where(sdchild.c.parent_id == 1000)
            .union(select(sdchild.c.id, sdchild.c.parent_id, sdchild.c.deleted_at).where(sdchild.c.parent_id == 1001))
        )
    )

    # Verify it's a LambdaElement with CompoundSelect inside
    assert isinstance(stmt, LambdaElement)
    assert isinstance(stmt._resolved, CompoundSelect)

    # Rewrite the statement
    rewritten = rewriter.rewrite_statement(stmt)

    # Verify soft-delete filters were applied to both selects in the union
    assert is_filtering_for_softdeleted(rewritten._resolved, {SDChild.__table__}) is True

    # Execute - should only return non-deleted children from both parents
    result = seeded_session.execute(rewritten)
    rows = result.fetchall()

    # All rows should have deleted_at = None
    for row in rows:
        assert row.deleted_at is None, f"Child {row.id} should not be deleted"
        assert row.parent_id in (1000, 1001), f"Child {row.id} should have parent_id in (1000, 1001)"


def test_lambda_stmt_with_include_deleted_option(seeded_session, rewriter):
    """Test that include_deleted execution option works with lambda_stmt.

    When the resolved Select has include_deleted=True, soft-delete filtering
    should be skipped.
    """
    # Create a lambda_stmt with include_deleted option
    stmt = lambda_stmt(lambda: select(SDParent).execution_options(include_deleted=True))

    # Rewrite the statement
    rewritten = rewriter.rewrite_statement(stmt)

    # Execute - should include deleted parent 1002
    result = seeded_session.execute(rewritten)
    parents = result.scalars().all()

    parent_ids = [p.id for p in parents]
    assert 1000 in parent_ids
    assert 1001 in parent_ids
    assert 1002 in parent_ids, "Deleted parent 1002 should be included with include_deleted=True"


# ==============================================================================
# Tests for lambda_stmt combined with eager loading strategies
# ==============================================================================


def test_lambda_stmt_with_joinedload(seeded_session, rewriter):
    """Test lambda_stmt combined with joinedload eager loading.

    The soft-delete filter should be applied to both the parent query
    and the eagerly loaded children via with_loader_criteria.

    Test data: Parent 1000 has 4 active children + 1 deleted child (100004)
    """
    stmt = lambda_stmt(lambda: select(SDParent).options(joinedload(SDParent.children)).where(SDParent.id == 1000))

    # Execute through session (hook will rewrite)
    result = seeded_session.execute(stmt)
    parent = result.scalars().unique().first()

    assert parent is not None
    assert parent.id == 1000

    # Children should only include non-deleted ones
    # Parent 1000 has children: 100000, 100001, 100002, 100003 (active) and 100004 (deleted)
    child_ids = [c.id for c in parent.children]
    assert 100000 in child_ids
    assert 100001 in child_ids
    assert 100002 in child_ids
    assert 100003 in child_ids
    assert 100004 not in child_ids, "Deleted child 100004 should not be in joinedload results"

    # Verify all loaded children are non-deleted
    for child in parent.children:
        assert child.deleted_at is None, f"Child {child.id} should not be deleted"


def test_lambda_stmt_with_selectinload(seeded_session, rewriter):
    """Test lambda_stmt combined with selectinload eager loading.

    selectinload uses a separate IN query to load related records.
    Soft-delete filtering should apply to this secondary query as well.

    Test data: Parent 1001 has 1 active child (100100) + 2 deleted children (100101, 100102)
    """
    stmt = lambda_stmt(lambda: select(SDParent).options(selectinload(SDParent.children)).where(SDParent.id == 1001))

    result = seeded_session.execute(stmt)
    parent = result.scalars().unique().first()

    assert parent is not None
    assert parent.id == 1001

    # Only the active child should be loaded
    assert len(parent.children) == 1, f"Expected 1 active child, got {len(parent.children)}"
    assert parent.children[0].id == 100100
    assert parent.children[0].deleted_at is None


def test_lambda_stmt_with_subqueryload(seeded_session, rewriter):
    """Test lambda_stmt combined with subqueryload eager loading.

    subqueryload uses a subquery to load all related records.
    """
    stmt = lambda_stmt(lambda: select(SDParent).options(subqueryload(SDParent.children)).where(SDParent.id == 1000))

    result = seeded_session.execute(stmt)
    parent = result.scalars().unique().first()

    assert parent is not None
    assert len(parent.children) == 4  # Only active children

    for child in parent.children:
        assert child.deleted_at is None


def test_lambda_stmt_with_nested_joinedload(seeded_session, rewriter):
    """Test lambda_stmt with chained/nested joinedload (Parent -> Children -> Grandchildren).

    Soft-delete filtering should be applied at all levels of the hierarchy.

    Test data: Parent 1000, Child 100000 has grandchildren:
    - Active: 10000000, 10000001, 10000002, 10000003
    - Deleted: 10000004, 10000005, 10000006
    """
    stmt = lambda_stmt(
        lambda: (
            select(SDParent)
            .options(joinedload(SDParent.children).joinedload(SDChild.child_children))
            .where(SDParent.id == 1000)
        )
    )

    result = seeded_session.execute(stmt)
    parent = result.scalars().unique().first()

    assert parent is not None
    assert parent.id == 1000

    # Verify children are filtered
    for child in parent.children:
        assert child.deleted_at is None, f"Child {child.id} should not be deleted"

        # Verify grandchildren are also filtered
        for grandchild in child.child_children:
            assert grandchild.deleted_at is None, (
                f"Grandchild {grandchild.id} of child {child.id} should not be deleted"
            )

    # Specifically check child 100000's grandchildren
    child_100000 = next((c for c in parent.children if c.id == 100000), None)
    assert child_100000 is not None
    grandchild_ids = [gc.id for gc in child_100000.child_children]

    # Active grandchildren should be present
    assert 10000000 in grandchild_ids
    assert 10000001 in grandchild_ids
    assert 10000002 in grandchild_ids
    assert 10000003 in grandchild_ids

    # Deleted grandchildren should NOT be present
    assert 10000004 not in grandchild_ids, "Deleted grandchild 10000004 should not be loaded"
    assert 10000005 not in grandchild_ids, "Deleted grandchild 10000005 should not be loaded"
    assert 10000006 not in grandchild_ids, "Deleted grandchild 10000006 should not be loaded"


def test_lambda_stmt_with_mixed_eager_loading(seeded_session, rewriter):
    """Test lambda_stmt with mixed eager loading strategies.

    Uses joinedload for children and selectinload for grandchildren.
    """
    stmt = lambda_stmt(
        lambda: (
            select(SDParent)
            .options(joinedload(SDParent.children).selectinload(SDChild.child_children))
            .where(SDParent.id == 1000)
        )
    )

    result = seeded_session.execute(stmt)
    parent = result.scalars().unique().first()

    assert parent is not None

    for child in parent.children:
        assert child.deleted_at is None
        for grandchild in child.child_children:
            assert grandchild.deleted_at is None


def test_lambda_stmt_with_chained_lambda_and_joinedload(seeded_session, rewriter):
    """Test LinkedLambdaElement (chained lambdas) with eager loading.

    This tests the case where lambda_stmt is extended with additional
    lambda operations and combined with eager loading.
    """
    # Start with base lambda
    stmt = lambda_stmt(lambda: select(SDParent).options(joinedload(SDParent.children)))

    # Chain additional filter via lambda
    stmt = stmt + (lambda s: s.where(SDParent.id.in_([1000, 1001])))

    # Verify it's a LinkedLambdaElement
    assert isinstance(stmt, LinkedLambdaElement)

    result = seeded_session.execute(stmt)
    parents = result.scalars().unique().all()

    assert len(parents) == 2

    for parent in parents:
        # Verify all loaded children are non-deleted
        for child in parent.children:
            assert child.deleted_at is None, f"Child {child.id} of parent {parent.id} should not be deleted"


def test_lambda_stmt_with_multiple_eager_loading_options(seeded_session, rewriter):
    """Test lambda_stmt with multiple separate eager loading options.

    Tests loading children via joinedload and also eager loading grandchildren
    through a separate path.
    """
    stmt = lambda_stmt(
        lambda: (
            select(SDParent)
            .options(
                joinedload(SDParent.children),
                joinedload(SDParent.children).joinedload(SDChild.child_children),
            )
            .where(SDParent.id == 1000)
        )
    )

    result = seeded_session.execute(stmt)
    parent = result.scalars().unique().first()

    assert parent is not None

    # Both levels should be loaded and filtered
    assert len(parent.children) == 4  # 4 active children

    total_grandchildren = sum(len(c.child_children) for c in parent.children)
    assert total_grandchildren > 0, "Should have some grandchildren loaded"

    # All should be non-deleted
    for child in parent.children:
        assert child.deleted_at is None
        for gc in child.child_children:
            assert gc.deleted_at is None


def test_lambda_stmt_eager_loading_with_filter_on_child(seeded_session, rewriter):
    """Test lambda_stmt with eager loading combined with a filter condition.

    Filters parents based on a child condition while still applying soft-delete.
    """
    stmt = lambda_stmt(
        lambda: (
            select(SDParent).join(SDChild).options(joinedload(SDParent.children)).where(SDChild.id == 100000)
        )  # Filter to get parent of child 100000
    )

    result = seeded_session.execute(stmt)
    parent = result.scalars().unique().first()

    assert parent is not None
    assert parent.id == 1000  # Child 100000 belongs to parent 1000

    # Children loaded via joinedload should still be soft-delete filtered
    for child in parent.children:
        assert child.deleted_at is None


def test_lambda_stmt_with_eager_loading_all_parents(seeded_session, rewriter):
    """Test lambda_stmt eager loading across multiple parents.

    Verifies that when loading multiple parents with eager loading,
    soft-delete filtering works correctly for all of them.
    """
    stmt = lambda_stmt(lambda: select(SDParent).options(joinedload(SDParent.children)))

    result = seeded_session.execute(stmt)
    parents = result.scalars().unique().all()

    # Should only get parents 1000 and 1001 (1002 is deleted)
    parent_ids = [p.id for p in parents]
    assert 1000 in parent_ids
    assert 1001 in parent_ids
    assert 1002 not in parent_ids, "Deleted parent 1002 should not appear"

    # Count total children - all should be non-deleted
    total_children = 0
    for parent in parents:
        for child in parent.children:
            assert child.deleted_at is None, f"Child {child.id} should not be deleted"
            total_children += 1

    # Parent 1000: 4 active children, Parent 1001: 1 active child = 5 total
    assert total_children == 5, f"Expected 5 active children total, got {total_children}"


def test_lambda_stmt_selectinload_nested_grandchildren(seeded_session, rewriter):
    """Test lambda_stmt with selectinload for nested grandchildren.

    Uses selectinload for both levels to test that soft-delete filtering
    works with the IN-based loading strategy at multiple levels.

    Test data for child 100001:
    - Active grandchildren: 10000100, 10000101, 10000102, 10000103, 10000104 (5 total)
    - Deleted grandchildren: 10000105, 10000106, 10000107, 10000108 (4 total)
    """
    stmt = lambda_stmt(
        lambda: (
            select(SDParent)
            .options(selectinload(SDParent.children).selectinload(SDChild.child_children))
            .where(SDParent.id == 1000)
        )
    )

    result = seeded_session.execute(stmt)
    parent = result.scalars().unique().first()

    assert parent is not None

    # Check child 100001 which has 5 active + 4 deleted grandchildren
    child_100001 = next((c for c in parent.children if c.id == 100001), None)
    assert child_100001 is not None

    grandchild_ids = [gc.id for gc in child_100001.child_children]

    # Active grandchildren should be present
    assert 10000100 in grandchild_ids
    assert 10000101 in grandchild_ids
    assert 10000102 in grandchild_ids
    assert 10000103 in grandchild_ids
    assert 10000104 in grandchild_ids

    # Deleted grandchildren should NOT be present
    assert 10000105 not in grandchild_ids, "Deleted grandchild 10000105 should be filtered"
    assert 10000106 not in grandchild_ids, "Deleted grandchild 10000106 should be filtered"
    assert 10000107 not in grandchild_ids, "Deleted grandchild 10000107 should be filtered"
    assert 10000108 not in grandchild_ids, "Deleted grandchild 10000108 should be filtered"

    assert len(child_100001.child_children) == 5, (
        f"Child 100001 should have 5 active grandchildren, got {len(child_100001.child_children)}"
    )


def test_lambda_stmt_chained_operations_with_order_and_eager_load(seeded_session, rewriter):
    """Test lambda_stmt with chained operations including ordering and eager loading.

    Combines multiple chained lambda operations with eager loading to test
    complex query building scenarios.
    """
    # Build query incrementally with chained lambdas
    stmt = lambda_stmt(lambda: select(SDParent))
    stmt = stmt + (lambda s: s.options(joinedload(SDParent.children)))
    stmt = stmt + (lambda s: s.where(SDParent.id.in_([1000, 1001])))
    stmt = stmt + (lambda s: s.order_by(SDParent.id.desc()))

    assert isinstance(stmt, LinkedLambdaElement)

    result = seeded_session.execute(stmt)
    parents = result.scalars().unique().all()

    # Should be ordered by id descending
    assert len(parents) == 2
    assert parents[0].id == 1001  # First due to DESC order
    assert parents[1].id == 1000

    # Verify soft-delete filtering on children
    for parent in parents:
        for child in parent.children:
            assert child.deleted_at is None


def test_lambda_stmt_with_func_count_and_join(seeded_session, rewriter):
    """Test lambda_stmt with aggregate function and join.

    Tests that soft-delete filtering works with aggregate queries
    wrapped in lambda_stmt.
    """
    stmt = lambda_stmt(lambda: select(func.count()).select_from(SDChild).join(SDParent))

    result = seeded_session.execute(stmt)
    count = result.scalar()

    # Total active children of active parents:
    # Parent 1000: 4 active children
    # Parent 1001: 1 active child
    # Parent 1002: deleted, so its children shouldn't count
    # Total: 5
    assert count == 5, f"Expected 5 active children of active parents, got {count}"


def test_lambda_stmt_eager_load_with_include_deleted_disables_all_filtering(seeded_session, rewriter):
    """Test that include_deleted=True disables ALL soft-delete filtering.

    When include_deleted is set on the main Select, the entire soft-delete
    rewriting is skipped - this includes both the main query AND eager loaded
    relations. This is the expected behavior because:
    1. The check happens early in rewrite_select()
    2. If include_deleted=True, we return immediately without adding any filters
    3. This also skips the eager loading criteria addition

    Test data:
    - Parent 1002 is deleted
    - Child 100004 of parent 1000 is deleted
    """
    stmt = lambda_stmt(
        lambda: select(SDParent).options(joinedload(SDParent.children)).execution_options(include_deleted=True)
    )

    result = seeded_session.execute(stmt)
    parents = result.scalars().unique().all()

    # Should include all 3 parents (including deleted 1002)
    parent_ids = [p.id for p in parents]
    assert 1000 in parent_ids
    assert 1001 in parent_ids
    assert 1002 in parent_ids, "Deleted parent 1002 should be included with include_deleted=True"

    # With include_deleted=True, ALL soft-delete filtering is disabled,
    # including for eager loads. So deleted children will also be loaded.
    parent_1000 = next(p for p in parents if p.id == 1000)
    child_ids = [c.id for c in parent_1000.children]

    # Child 100004 is deleted, but should be loaded because filtering is disabled
    assert 100004 in child_ids, "Deleted child 100004 should be loaded when include_deleted=True"


def test_lambda_stmt_joinedload_adds_loader_criteria_option(rewriter):
    """Verify that rewriting lambda_stmt with joinedload adds LoaderCriteriaOption.

    This is a regression test ensuring that the eager loading fix
    works correctly with lambda_stmt. The _add_loader_criteria_for_eager_loads()
    method should detect eager loading options on the resolved Select and add
    with_loader_criteria for soft-deletable entities.

    The LoaderCriteriaOption adds the soft-delete filter to the JOIN ON clause,
    which is essential for correctly filtering eagerly loaded relations.
    """
    stmt = lambda_stmt(lambda: select(SDParent).options(joinedload(SDParent.children)))

    # Before rewrite: only the Load option from joinedload
    resolved_before = stmt._resolved
    options_before = resolved_before._with_options
    assert len(options_before) == 1, "Should have 1 option (Load) before rewrite"

    # Rewrite
    rewritten = rewriter.rewrite_statement(stmt)

    # After rewrite: Load + LoaderCriteriaOption(s) for soft-deletable entities
    resolved_after = rewritten._resolved
    options_after = resolved_after._with_options

    # Should have more options now (LoaderCriteriaOption added)
    assert len(options_after) > 1, "Should have additional LoaderCriteriaOption after rewrite"

    # Verify LoaderCriteriaOption was added
    loader_criteria_options = [opt for opt in options_after if isinstance(opt, LoaderCriteriaOption)]
    assert len(loader_criteria_options) > 0, "LoaderCriteriaOption should be added for soft-deletable entities"


def test_lambda_stmt_nested_joinedload_adds_criteria_for_all_levels(rewriter):
    """Verify nested joinedload adds LoaderCriteriaOption for all eager loaded entities.

    When using chained joinedload (Parent -> Children -> Grandchildren),
    LoaderCriteriaOption should be added for both SDChild and SDChildChild.
    """
    stmt = lambda_stmt(
        lambda: select(SDParent).options(joinedload(SDParent.children).joinedload(SDChild.child_children))
    )

    rewritten = rewriter.rewrite_statement(stmt)
    resolved = rewritten._resolved

    loader_criteria_options = [opt for opt in resolved._with_options if isinstance(opt, LoaderCriteriaOption)]

    # Should have criteria for: SDParent (main entity), SDChild, SDChildChild
    # The exact count may vary but should be >= 2 (for Child and ChildChild)
    assert len(loader_criteria_options) >= 2, (
        f"Expected at least 2 LoaderCriteriaOption for nested eager loading, got {len(loader_criteria_options)}"
    )
