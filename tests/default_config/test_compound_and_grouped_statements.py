"""Unions with members in parentheses, include_deleted on a union or from_statement() itself, and
JOINs nested on the right of another JOIN (held in parentheses)."""

from typing import Any

import pytest
from sqlalchemy import select, union, union_all
from sqlalchemy.orm import Session

from tests.default_config.model import SDChild, SDChildChild, SDParent


def _active_child_ids(session: Session) -> list[int]:
    return sorted(
        session.scalars(select(SDChild.id).where(SDChild.deleted_at.is_(None)).execution_options(include_deleted=True))
    )


def _all_child_ids(session: Session) -> list[int]:
    return sorted(session.scalars(select(SDChild.id).execution_options(include_deleted=True)))


def _ids(session: Session, stmt: Any) -> list[int]:
    return sorted(row[0] for row in session.execute(stmt))


def _skip_on_sqlite(db_connection: Any) -> None:
    # SQLite does not accept parenthesized SELECTs inside a UNION (see CI: PostgreSQL).
    if db_connection.dialect.name == "sqlite":
        pytest.skip("SQLite does not support parenthesized members in a UNION")


def test_union_member_with_limit_is_rewritten(rewriter):
    stmt = union_all(select(SDChild.id).order_by(SDChild.id).limit(100), select(SDChildChild.id))

    sql = str(rewriter.rewrite_statement(stmt))

    assert "sdchild.deleted_at IS NULL" in sql
    assert "sdchildchild.deleted_at IS NULL" in sql


def test_nested_union_is_rewritten(rewriter):
    stmt = union(union(select(SDChild.id), select(SDParent.id)), select(SDChildChild.id))

    sql = str(rewriter.rewrite_statement(stmt))

    for table in ("sdchild", "sdparent", "sdchildchild"):
        assert f"{table}.deleted_at IS NULL" in sql


def test_union_member_with_limit_is_filtered(seeded_session, db_connection):
    _skip_on_sqlite(db_connection)
    stmt = union_all(select(SDChild.id).order_by(SDChild.id).limit(100), select(SDChild.id).where(SDChild.id < 0))

    assert _ids(seeded_session, stmt) == _active_child_ids(seeded_session)


def test_nested_union_is_filtered(seeded_session, db_connection):
    _skip_on_sqlite(db_connection)
    stmt = union(union(select(SDChild.id), select(SDChild.id).where(SDChild.id < 0)), select(SDChild.id))

    assert _ids(seeded_session, stmt) == _active_child_ids(seeded_session)


def test_include_deleted_on_a_union_is_honored(seeded_session):
    stmt = union(select(SDChild.id), select(SDChild.id).where(SDChild.id < 0)).execution_options(include_deleted=True)

    assert _ids(seeded_session, stmt) == _all_child_ids(seeded_session)


def test_include_deleted_on_a_from_statement_is_honored(rewriter):
    stmt = select(SDChild).from_statement(select(SDChild)).execution_options(include_deleted=True)

    assert rewriter.rewrite_statement(stmt) is stmt


def test_join_nested_on_the_right_is_filtered(seeded_session):
    grandchild, child, parent = SDChildChild.__table__, SDChild.__table__, SDParent.__table__
    nested = grandchild.join(child.join(parent, child.c.parent_id == parent.c.id), grandchild.c.child_id == child.c.id)
    stmt = select(grandchild.c.id).select_from(nested)

    expected = (
        select(SDChildChild.id)
        .join(SDChildChild.child)
        .join(SDChild.parent)
        .where(SDChildChild.deleted_at.is_(None), SDChild.deleted_at.is_(None), SDParent.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    assert _ids(seeded_session, stmt) == _ids(seeded_session, expected)
