"""LATERAL subqueries (``select(...).lateral()``) are filtered like other subqueries. A join to a subquery
whose ON clause names none of its columns (``ON true``, the usual way to join a LATERAL subquery) gets the
left side SQLAlchemy found for the statement."""

import pytest
from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from tests.default_config.model import SDChild, SDParent


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


def test_lateral_subquery_selected_from_without_a_join(lateral_session):
    latest = _latest_child()

    rows = lateral_session.execute(select(SDParent.id, latest.c.id)).all()

    expected = sorted((parent_id, max(ids)) for parent_id, ids in _active_children_by_parent(lateral_session).items())
    assert sorted(tuple(row) for row in rows) == expected


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
