"""Rewriting a statement must never modify it, nor anything inside it.

Applications build statements once and execute them many times (module-level subqueries, unions,
``lambda_stmt``). Each execution must be filtered the same way, and ``include_deleted`` must keep
working on statements built from objects that earlier executions used.
"""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import lambda_stmt, select, union_all
from sqlalchemy.orm import Session, aliased

from tests.default_config.model import SDSimpleTable

ACTIVE_ID, DELETED_ID = 1, 2


@pytest.fixture
def simple_rows(db_session: Session) -> Session:
    active = SDSimpleTable(id=ACTIVE_ID, int_field=10)
    deleted = SDSimpleTable(id=DELETED_ID, int_field=20)
    db_session.add_all([active, deleted])
    db_session.flush()
    deleted.delete()
    db_session.flush()
    return db_session


def _subquery() -> Any:
    return select(SDSimpleTable.id).subquery()


def _aliased_subquery() -> Any:
    return aliased(SDSimpleTable, select(SDSimpleTable).subquery())


# Each case: (builds the shared object, builds a statement on top of that object)
SHARED_FROM_OBJECTS: dict[str, tuple[Callable[[], Any], Callable[[Any], Any]]] = {
    "subquery_in_join": (_subquery, lambda sq: select(SDSimpleTable.id).join(sq, sq.c.id == SDSimpleTable.id)),
    "subquery_columns": (_subquery, lambda sq: select(sq.c.id)),
    "subquery_select_from": (_subquery, lambda sq: select(sq.c.id).select_from(sq)),
    "aliased_subquery_entity": (_aliased_subquery, lambda alias: select(alias)),
    "aliased_subquery_column": (_aliased_subquery, lambda alias: select(alias.id)),
}


def _ids(session: Session, stmt: Any) -> list[int]:
    return sorted(row if isinstance(row, int) else row.id for row in session.scalars(stmt))


@pytest.mark.parametrize("make_shared, make_statement", SHARED_FROM_OBJECTS.values(), ids=SHARED_FROM_OBJECTS.keys())
def test_reused_from_object_is_filtered_the_same_every_time(simple_rows, make_shared, make_statement):
    shared = make_shared()
    sql_before = str(make_statement(shared))

    for _ in range(3):
        assert _ids(simple_rows, make_statement(shared)) == [ACTIVE_ID]

    assert str(make_statement(shared)) == sql_before


@pytest.mark.parametrize("make_shared, make_statement", SHARED_FROM_OBJECTS.values(), ids=SHARED_FROM_OBJECTS.keys())
def test_include_deleted_works_on_a_from_object_a_filtered_execution_used(simple_rows, make_shared, make_statement):
    shared = make_shared()
    assert _ids(simple_rows, make_statement(shared)) == [ACTIVE_ID]

    unfiltered = make_statement(shared).execution_options(include_deleted=True)

    assert _ids(simple_rows, unfiltered) == [ACTIVE_ID, DELETED_ID]


def test_reused_union_is_not_modified(simple_rows):
    stmt = union_all(select(SDSimpleTable.id), select(SDSimpleTable.id))
    sql_before = str(stmt)

    for _ in range(3):
        assert sorted(simple_rows.scalars(stmt)) == [ACTIVE_ID, ACTIVE_ID]

    assert str(stmt) == sql_before


def test_reused_query_union_is_not_modified(simple_rows):
    query = simple_rows.query(SDSimpleTable).union(simple_rows.query(SDSimpleTable))
    sql_before = str(query.statement)

    for _ in range(3):
        assert [row.id for row in query.all()] == [ACTIVE_ID]

    assert str(query.statement) == sql_before
    assert sorted(row.id for row in query.execution_options(include_deleted=True).all()) == [ACTIVE_ID, DELETED_ID]


def test_reused_from_statement_is_not_modified(simple_rows, rewriter):
    """Checked through the rewriter: SQLAlchemy 2.0.10 does not report a from_statement() to the
    do_orm_execute hook as a SELECT, so the hook only rewrites it on later versions."""
    stmt = select(SDSimpleTable).from_statement(select(SDSimpleTable))
    sql_before = str(stmt)

    rewritten = rewriter.rewrite_statement(stmt)
    results = [sorted(row.id for row in simple_rows.scalars(stmt)) for _ in range(3)]

    assert "sdsimpletable.deleted_at IS NULL" in str(rewritten)
    assert str(stmt) == sql_before
    assert results[0] == results[1] == results[2]


def test_reused_lambda_stmt_is_not_modified(simple_rows):
    stmt = lambda_stmt(lambda: select(SDSimpleTable))
    resolved_sql_before = str(stmt._resolved)

    for _ in range(3):
        assert [row.id for row in simple_rows.scalars(stmt)] == [ACTIVE_ID]

    assert str(stmt._resolved) == resolved_sql_before


def test_rewrite_returns_a_new_statement_and_keeps_the_given_one(rewriter):
    sq = _subquery()
    stmt = select(SDSimpleTable.id).join(sq, sq.c.id == SDSimpleTable.id)
    stmt_sql, subquery_sql = str(stmt), str(sq)

    rewritten = rewriter.rewrite_statement(stmt)

    assert rewritten is not stmt
    assert "deleted_at IS NULL" in str(rewritten)
    assert (str(stmt), str(sq)) == (stmt_sql, subquery_sql)


def _all_rows_lambda() -> Any:
    return lambda_stmt(lambda: select(SDSimpleTable.id))


def _rows_with_id_lambda(row_id: int) -> Any:
    return lambda_stmt(lambda: select(SDSimpleTable.id).where(SDSimpleTable.id == row_id))


def test_lambda_stmt_run_through_core_first_is_still_filtered_through_the_session(simple_rows, db_connection):
    """SQLAlchemy caches a lambda statement's compiled form by the lambda's code: an execution that
    the hook does not rewrite (Core) must not hand its compiled form to the session's executions.
    """
    assert sorted(row[0] for row in db_connection.execute(_all_rows_lambda())) == [ACTIVE_ID, DELETED_ID]

    assert sorted(simple_rows.scalars(_all_rows_lambda())) == [ACTIVE_ID]
    assert sorted(row[0] for row in db_connection.execute(_all_rows_lambda())) == [ACTIVE_ID, DELETED_ID]


def test_lambda_stmt_parameters_are_those_of_each_execution(simple_rows):
    for row_id, expected in ((ACTIVE_ID, [ACTIVE_ID]), (DELETED_ID, []), (ACTIVE_ID, [ACTIVE_ID])):
        assert sorted(simple_rows.scalars(_rows_with_id_lambda(row_id))) == expected
