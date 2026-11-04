"""``select(...).from_statement(select(...))`` executed through the session is filtered on every supported
SQLAlchemy release. Before 2.0.30, SQLAlchemy does not report it to ``do_orm_execute`` hooks as a SELECT.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

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


def test_executed_from_statement_eager_loads_filtered_children(seeded_session):
    seeded_session.expunge_all()
    stmt = select(SDParent).options(selectinload(SDParent.children)).from_statement(select(SDParent))

    parents = seeded_session.scalars(stmt).all()
    children = [child for parent in parents for child in parent.children]

    assert parents
    assert all(parent.deleted_at is None for parent in parents)
    assert sorted(child.id for child in children) == sorted(
        child_id
        for parent_id, child_id in seeded_session.execute(
            select(SDChild.parent_id, SDChild.id)
            .join(SDChild.parent)
            .where(SDChild.deleted_at.is_(None), SDParent.deleted_at.is_(None))
            .execution_options(include_deleted=True)
        )
    )
