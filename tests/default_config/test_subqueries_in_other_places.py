"""A subquery the statement selects from is filtered wherever else the statement uses it: in a join whose
left side SQLAlchemy finds itself, in SELECTs nested in the statement, and when it is a clone (from
``.params()`` or ``lambda_stmt()``)."""

from typing import Any

import pytest
from sqlalchemy import and_, bindparam, func, lambda_stmt, select
from sqlalchemy.orm import Session, aliased

from tests.default_config.model import SDChild, SDParent


def _active_counts() -> Any:
    return select(SDChild.parent_id, func.count().label("n")).group_by(SDChild.parent_id).subquery()


def _expected_counts(session: Session) -> dict[int, int]:
    stmt = (
        select(SDChild.parent_id, func.count())
        .where(SDChild.deleted_at.is_(None))
        .group_by(SDChild.parent_id)
        .execution_options(include_deleted=True)
    )
    return {parent_id: count for parent_id, count in session.execute(stmt).all()}


def _active_child_ids_of_active_parents(session: Session) -> list[int]:
    stmt = (
        select(SDChild.id)
        .join(SDChild.parent)
        .where(SDChild.deleted_at.is_(None), SDParent.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    return sorted(session.scalars(stmt))


def test_join_with_an_implicit_left_side_next_to_a_subquery_in_the_columns(seeded_session):
    counts = _active_counts()
    stmt = select(SDParent.id, counts.c.n, SDChild.id).where(counts.c.parent_id == SDParent.id).join(SDChild)

    rows = seeded_session.execute(stmt).all()

    expected = _expected_counts(seeded_session)
    assert sorted(child_id for _, _, child_id in rows) == _active_child_ids_of_active_parents(seeded_session)
    assert all(count == expected[parent_id] for parent_id, count, _ in rows)


def test_outer_join_with_an_implicit_left_side_next_to_a_subquery_in_the_columns(seeded_session):
    counts = _active_counts()
    stmt = (
        select(SDParent.id, counts.c.n, SDChild.id)
        .where(counts.c.parent_id == SDParent.id)
        .outerjoin(SDChild, and_(SDChild.parent_id == SDParent.id, SDChild.id >= 100100))
    )

    rows = sorted(seeded_session.execute(stmt).all())

    assert rows == [(1000, 4, None), (1001, 1, 100100)]


def test_join_with_an_implicit_left_side_next_to_an_aliased_subquery(seeded_session):
    alias = aliased(SDChild, select(SDChild).subquery())
    stmt = select(SDParent.id, alias.id).where(alias.parent_id == SDParent.id).join(SDChild)

    rows = seeded_session.execute(stmt).all()

    assert sorted({alias_id for _, alias_id in rows}) == _active_child_ids_of_active_parents(seeded_session)


def test_scalar_subquery_over_the_statements_own_subquery(seeded_session):
    """Rows with the largest count: the nested SELECT reads the same (filtered) subquery."""
    counts = _active_counts()
    stmt = select(counts.c.parent_id, counts.c.n).where(counts.c.n == select(func.max(counts.c.n)).scalar_subquery())

    rows = seeded_session.execute(stmt).all()

    expected = _expected_counts(seeded_session)
    assert rows == [(parent_id, n) for parent_id, n in expected.items() if n == max(expected.values())]


def test_scalar_subquery_over_the_statements_own_subquery_in_the_columns(seeded_session):
    counts = _active_counts()
    total = select(func.sum(counts.c.n)).scalar_subquery().label("total")

    rows = seeded_session.execute(select(counts.c.parent_id, total)).all()

    assert {row.total for row in rows} == {sum(_expected_counts(seeded_session).values())}


def _counts_with_a_parameter() -> Any:
    return (
        select(SDChild.parent_id, func.count().label("n"))
        .where(SDChild.id > bindparam("min_id", 0))
        .group_by(SDChild.parent_id)
        .subquery()
    )


@pytest.mark.parametrize("outer", [False, True], ids=["join", "outerjoin"])
def test_join_to_a_subquery_of_a_statement_with_params(seeded_session, outer):
    counts = _counts_with_a_parameter()
    stmt = select(SDParent.id, counts.c.n)
    stmt = (
        stmt.outerjoin(counts, counts.c.parent_id == SDParent.id)
        if outer
        else stmt.join(counts, counts.c.parent_id == SDParent.id)
    )

    rows = dict(seeded_session.execute(stmt.params(min_id=0)).all())

    expected = _expected_counts(seeded_session)
    assert rows == {parent_id: n for parent_id, n in expected.items() if parent_id != 1002}


def test_join_to_a_subquery_in_a_lambda_statement_with_a_closure_variable(seeded_session):
    counts = _counts_with_a_parameter()
    min_parent = 0
    stmt = lambda_stmt(
        lambda: (
            select(SDParent.id, counts.c.n)
            .join(counts, counts.c.parent_id == SDParent.id)
            .where(SDParent.id >= min_parent)
        )
    )

    rows = dict(seeded_session.execute(stmt).all())

    expected = _expected_counts(seeded_session)
    assert rows == {parent_id: n for parent_id, n in expected.items() if parent_id != 1002}


def test_relationship_join_to_an_aliased_subquery_with_params(seeded_session):
    alias = aliased(SDChild, select(SDChild).where(SDChild.id > bindparam("min_id", 0)).subquery().params(min_id=0))
    stmt = select(alias.id).select_from(SDParent).join(SDParent.children.of_type(alias))

    assert sorted(seeded_session.scalars(stmt)) == _active_child_ids_of_active_parents(seeded_session)
