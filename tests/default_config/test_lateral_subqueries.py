"""LATERAL subqueries (``select(...).lateral()``) are filtered like other subqueries. A join to a subquery
whose ON clause names none of its columns (``ON true``, the usual way to join a LATERAL subquery) gets the
left side SQLAlchemy found for the statement."""

from datetime import datetime

import pytest
from sqlalchemy import exists, func, literal, select, true
from sqlalchemy.orm import Session

from tests.default_config.model import SDChild, SDChildChild, SDParent


@pytest.fixture
def lateral_session(seeded_session, db_connection):
    if db_connection.dialect.name == "sqlite":
        pytest.skip("SQLite does not support LATERAL")
    return seeded_session


def _active_children_by_parent(session: Session, *, include_deleted_children: bool = False) -> dict[int, list[int]]:
    stmt = (
        select(SDParent.id, SDChild.id)
        .join(SDChild, SDChild.parent_id == SDParent.id)
        .where(SDParent.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    if not include_deleted_children:
        stmt = stmt.where(SDChild.deleted_at.is_(None))
    by_parent: dict[int, list[int]] = {}
    for parent_id, child_id in session.execute(stmt):
        by_parent.setdefault(parent_id, []).append(child_id)
    return by_parent


def _latest_child(*, include_deleted: bool = False):
    inner = select(SDChild.id).where(SDChild.parent_id == SDParent.id).order_by(SDChild.id.desc()).limit(1)
    if include_deleted:
        inner = inner.execution_options(include_deleted=True)
    return inner.lateral("latest_child")


@pytest.mark.parametrize("outer", [False, True], ids=["join", "outerjoin"])
def test_lateral_subquery_is_filtered(lateral_session, outer):
    expected = sorted((parent_id, max(ids)) for parent_id, ids in _active_children_by_parent(lateral_session).items())

    for _ in range(2):
        latest = _latest_child()
        stmt = select(SDParent.id, latest.c.id)
        stmt = stmt.outerjoin(latest, true()) if outer else stmt.join(latest, true())

        assert sorted(tuple(row) for row in lateral_session.execute(stmt)) == expected


def test_lateral_subquery_with_include_deleted(lateral_session):
    latest = _latest_child(include_deleted=True)

    rows = lateral_session.execute(select(SDParent.id, latest.c.id).join(latest, true())).all()

    by_parent = _active_children_by_parent(lateral_session, include_deleted_children=True)
    assert sorted(tuple(row) for row in rows) == sorted((parent_id, max(ids)) for parent_id, ids in by_parent.items())


def test_lateral_count_is_filtered(lateral_session):
    counts = select(func.count().label("n")).select_from(SDChild).where(SDChild.parent_id == SDParent.id).lateral()

    rows = lateral_session.execute(select(SDParent.id, counts.c.n).join(counts, true())).all()

    by_parent = _active_children_by_parent(lateral_session)
    assert sorted(tuple(row) for row in rows) == sorted((parent_id, len(ids)) for parent_id, ids in by_parent.items())


def test_join_to_a_subquery_on_a_condition_without_its_columns(seeded_session):
    children = select(SDChild.id).where(SDChild.parent_id == 1000).subquery()

    rows = seeded_session.execute(select(SDParent.id, children.c.id).join(children, true())).all()

    active_parents = seeded_session.scalars(
        select(SDParent.id).where(SDParent.deleted_at.is_(None)).execution_options(include_deleted=True)
    ).all()
    active_children = _active_children_by_parent(seeded_session)[1000]
    assert sorted(tuple(row) for row in rows) == sorted((p, c) for p in active_parents for c in active_children)


def test_lateral_subquery_selected_from_without_a_join_is_refused(lateral_session):
    """Its filtered copy cannot take its place in the FROM list, after the FROMs it refers to, in every shape:
    the statement is refused instead of returning soft-deleted rows."""
    latest = _latest_child()

    with pytest.raises(NotImplementedError, match="join it"):
        lateral_session.execute(select(SDParent.id, latest.c.id)).all()


def test_outer_join_to_a_lateral_subquery_keeps_parents_without_active_children(lateral_session):
    lateral_session.add(SDParent(id=3000))
    lateral_session.flush()
    lateral_session.add(SDChild(id=300000, parent_id=3000, deleted_at=datetime(2000, 1, 1)))
    lateral_session.flush()
    latest = _latest_child()

    rows = lateral_session.execute(select(SDParent.id, latest.c.id).outerjoin(latest, true())).all()

    expected = sorted((parent_id, max(ids)) for parent_id, ids in _active_children_by_parent(lateral_session).items())
    assert sorted(rows, key=repr) == sorted([*expected, (3000, None)], key=repr)


def test_lateral_subquery_referring_to_another_one(lateral_session):
    latest = _latest_child()
    grandchildren = (
        select(func.count().label("n"))
        .select_from(SDChildChild)
        .where(SDChildChild.child_id == latest.c.id)
        .lateral("grandchildren")
    )
    # Two joins ON true need select_from(): SQLAlchemy cannot place the second one otherwise.
    stmt = (
        select(SDParent.id, latest.c.id, grandchildren.c.n)
        .select_from(SDParent)
        .join(latest, true())
        .join(grandchildren, true())
    )

    rows = lateral_session.execute(stmt).all()

    expected = []
    for parent_id, ids in _active_children_by_parent(lateral_session).items():
        child_id = max(ids)
        count = lateral_session.scalar(
            select(func.count())
            .select_from(SDChildChild)
            .where(SDChildChild.child_id == child_id, SDChildChild.deleted_at.is_(None))
            .execution_options(include_deleted=True)
        )
        expected.append((parent_id, child_id, count))
    assert sorted(tuple(row) for row in rows) == sorted(expected)


def test_lateral_subquery_joined_on_its_own_columns(lateral_session):
    counts = select(func.count().label("n")).select_from(SDChild).where(SDChild.parent_id == SDParent.id).lateral()

    rows = lateral_session.execute(select(SDParent.id, counts.c.n).join(counts, counts.c.n > 0)).all()

    by_parent = _active_children_by_parent(lateral_session)
    assert sorted(tuple(row) for row in rows) == sorted((parent_id, len(ids)) for parent_id, ids in by_parent.items())


def test_join_to_a_subquery_on_its_own_columns_only(seeded_session):
    children = select(SDChild.id).where(SDChild.parent_id == 1000).subquery()

    rows = seeded_session.execute(select(SDParent.id, children.c.id).join(children, children.c.id > 0)).all()

    active_parents = seeded_session.scalars(
        select(SDParent.id).where(SDParent.deleted_at.is_(None)).execution_options(include_deleted=True)
    ).all()
    active_children = _active_children_by_parent(seeded_session)[1000]
    assert sorted(tuple(row) for row in rows) == sorted((p, c) for p in active_parents for c in active_children)


def test_join_to_a_subquery_on_an_exists_of_other_tables(seeded_session):
    """The tables of a SELECT nested in the ON clause do not tell SQLAlchemy the join's left side."""
    children = select(SDChild.id).where(SDChild.parent_id == 1000).subquery()
    any_child = exists(
        select(SDChild.id).where(SDChild.parent_id == SDParent.id).execution_options(include_deleted=True)
    )

    rows = seeded_session.execute(select(SDParent.id, children.c.id).join(children, any_child)).all()

    parents_with_children = seeded_session.scalars(
        select(SDParent.id)
        .where(SDParent.deleted_at.is_(None), SDParent.children.any())
        .execution_options(include_deleted=True)
    ).all()
    active_children = _active_children_by_parent(seeded_session)[1000]
    assert sorted(tuple(row) for row in rows) == sorted((p, c) for p in parents_with_children for c in active_children)


def test_join_on_true_to_a_subquery_that_needs_no_filter_stays_remembered(seeded_session, rewriter):
    """Only a join to a subquery that gets a filtered copy needs the left side SQLAlchemy found for the
    statement, which keeps its rewrite from being remembered."""
    numbers = select(literal(7).label("n")).subquery("numbers")
    stmt = select(SDParent.id, numbers.c.n).join(numbers, true())
    seeded_session.execute(stmt).all()
    before = rewriter.rewrite_cache_info()

    rows = seeded_session.execute(stmt).all()

    after = rewriter.rewrite_cache_info()
    assert sorted(rows) == [(parent_id, 7) for parent_id in sorted(seeded_session.scalars(select(SDParent.id)))]
    assert after.hits >= before.hits + 1
    assert after.uncacheable == before.uncacheable
