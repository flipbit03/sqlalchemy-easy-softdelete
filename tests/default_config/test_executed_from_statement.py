"""``select(...).from_statement(select(...))`` executed through the session is filtered on every supported
SQLAlchemy release, also through ``Query.from_statement()`` and inside ``lambda_stmt()``. Before 2.0.30,
SQLAlchemy does not report a ``from_statement()`` to ``do_orm_execute`` hooks as a SELECT.
"""

from sqlalchemy import lambda_stmt, select
from sqlalchemy.orm import Session, contains_eager

from tests.default_config.model import SDChild, SDParent


def _child_ids(session: Session, *, active_only: bool) -> list[int]:
    stmt = select(SDChild.id).execution_options(include_deleted=True)
    if active_only:
        stmt = stmt.where(SDChild.deleted_at.is_(None))
    return sorted(session.scalars(stmt))


def test_executed_from_statement_is_filtered(seeded_session):
    children = seeded_session.scalars(select(SDChild).from_statement(select(SDChild))).all()

    assert sorted(child.id for child in children) == _child_ids(seeded_session, active_only=True)


def test_executed_from_statement_with_include_deleted_is_not_filtered(seeded_session):
    stmt = select(SDChild).from_statement(select(SDChild)).execution_options(include_deleted=True)

    children = seeded_session.scalars(stmt).all()

    assert sorted(child.id for child in children) == _child_ids(seeded_session, active_only=False)


def test_query_from_statement_is_filtered(seeded_session):
    children = seeded_session.query(SDChild).from_statement(select(SDChild)).all()

    assert sorted(child.id for child in children) == _child_ids(seeded_session, active_only=True)


def test_lambda_stmt_of_a_from_statement_is_filtered(seeded_session):
    stmt = lambda_stmt(lambda: select(SDChild).from_statement(select(SDChild)))

    for _ in range(2):
        children = seeded_session.scalars(stmt).all()

        assert sorted(child.id for child in children) == _child_ids(seeded_session, active_only=True)


def test_lambda_stmt_of_a_from_statement_with_include_deleted_is_not_filtered(seeded_session):
    stmt = lambda_stmt(lambda: select(SDChild).from_statement(select(SDChild))).execution_options(include_deleted=True)

    children = seeded_session.scalars(stmt).all()

    assert sorted(child.id for child in children) == _child_ids(seeded_session, active_only=False)


def test_contains_eager_through_from_statement_loads_filtered_rows(seeded_session):
    seeded_session.expunge_all()
    joined = select(SDParent, SDChild).join(SDParent.children)
    stmt = select(SDParent).options(contains_eager(SDParent.children)).from_statement(joined)

    parents = seeded_session.scalars(stmt).unique().all()
    loaded = {parent.id: sorted(child.id for child in parent.children) for parent in parents}

    expected: dict[int, list[int]] = {}
    for parent_id, child_id in seeded_session.execute(
        select(SDParent.id, SDChild.id)
        .join(SDParent.children)
        .where(SDParent.deleted_at.is_(None), SDChild.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    ):
        expected.setdefault(parent_id, []).append(child_id)
    assert loaded == {parent_id: sorted(child_ids) for parent_id, child_ids in expected.items()}
