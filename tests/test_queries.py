"""Tests for `sqlalchemy_easy_softdelete` package."""
from typing import List

import pytest
from sqlalchemy import func, insert, select, table, text
from sqlalchemy.orm import Query
from sqlalchemy.sql import Select

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

    snapshot.assert_match(sorted(test_query.all(), key=lambda i: i.id))


def test_query_with_join(snapshot, seeded_session, rewriter):
    """Query with a simple join"""
    test_query: Query = seeded_session.query(SDChild).join(SDParent)  # noqa -- wrong typing stub in SA

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert (
        is_filtering_for_softdeleted(soft_deleted_rewritten_statement, {SDChild.__table__, SDParent.__table__}) is True
    )

    snapshot.assert_match(sorted(test_query.all(), key=lambda i: i.id))


def test_query_union_sdchild(snapshot, seeded_session, rewriter):
    """Two queries joined via UNION"""
    test_query: Query = seeded_session.query(SDChild).union(seeded_session.query(SDChild))

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert is_filtering_for_softdeleted(soft_deleted_rewritten_statement, {SDChild.__table__}) is True

    snapshot.assert_match(sorted(test_query.all(), key=lambda i: i.id))


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

    all_children: List[SDChild] = seeded_session.query(SDChild).execution_options(include_deleted=True).all()

    assert sorted(test_query.all(), key=lambda x: x.id) == sorted(all_children, key=lambda x: x.id)

    snapshot.assert_match(sorted(test_query.all(), key=lambda i: i.id))


def test_ensure_aggregate_from_multiple_table_deletion_works_active_object_count(snapshot, seeded_session, rewriter):
    """Aggregate function from a query that contains a join"""
    test_query: Query = seeded_session.query(SDChild).join(SDParent).with_entities(func.count())  # noqa

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert is_filtering_for_softdeleted(soft_deleted_rewritten_statement, {SDChild.__table__}) is True

    snapshot.assert_match(test_query.count())


def test_ensure_table_with_inheritance_works(snapshot, seeded_session, rewriter):
    test_query: Query = seeded_session.query(SDDerivedRequest)

    soft_deleted_rewritten_statement = rewriter.rewrite_statement(test_query.statement)

    assert is_filtering_for_softdeleted(soft_deleted_rewritten_statement, {SDBaseRequest.__table__}) is True

    test_query_results = test_query.all()
    assert len(test_query_results) == 2
    snapshot.assert_match(sorted(test_query_results, key=lambda i: i.id))

    all_active_and_deleted_derived_requests = (
        seeded_session.query(SDDerivedRequest).execution_options(include_deleted=True).all()
    )

    assert len(all_active_and_deleted_derived_requests) == 3
    snapshot.assert_match(sorted(all_active_and_deleted_derived_requests, key=lambda i: i.id))


def test_ensure_table_with_inheritance_works_query_base(snapshot, seeded_session, rewriter):
    """
    Querying for a polymorphic entity *without JOIN* should work when fields contained in
    derived entities are lazily fetched.
    """

    # Query the BASE entity, without joins.
    test_query: Query = seeded_session.query(SDBaseRequest).filter(SDBaseRequest.request_type == 'sdderivedrequest')

    request: SDDerivedRequest = test_query.first()

    try:
        # Accessing a field in a SDDerived Request will trigger an additional query with
        # a `FromStatement` as the statement, instead of a normal Select
        request.derived_field
    except Exception as exc:
        assert False, f"'Exception was raised {exc}"


def test_query_with_text_clause_as_table(snapshot, seeded_session, rewriter):
    """We cannot parse information from a literal text table name -- return unchanged"""

    # Table as a TextClause
    test_query_text_clause: Select = select(text('id')).select_from(text("sdderivedrequest"))
    snapshot.assert_match(str(rewriter.rewrite_statement(test_query_text_clause)))


def test_query_with_table_clause_as_table(snapshot, seeded_session, rewriter):
    """We cannot parse information from a literal text table name -- return unchanged"""

    # Table as a TableClause
    test_query_table_clause: Select = select(text('id')).select_from(table("sdderivedrequest"))
    snapshot.assert_match(str(rewriter.rewrite_statement(test_query_table_clause)))


def test_insert_with_returning(snapshot, seeded_session, rewriter, db_connection):
    """Insert with RETURNING is considered a *Select* by SQLAlchemy, since it returns data :dizzy:
    that means we need to actively protect against this case"""

    # RETURNING is not supported in SQLite
    if db_connection.dialect.name == 'sqlite':
        pytest.skip('SQLite does not support "INSERT...RETURNING"')

    insert_stmt = insert(SDSimpleTable).values(int_field=10).returning(SDSimpleTable)

    # Generate an Insert + RETURNING
    insert_returning = select(SDSimpleTable).from_statement(insert_stmt)

    result = seeded_session.execute(insert_returning)

    assert list(result)[0][0].int_field == 10


def test_query_with_more_than_one_join(snapshot, seeded_session, rewriter):
    test_query = (
        seeded_session.query(SDParent)
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
