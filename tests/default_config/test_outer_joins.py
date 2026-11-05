"""A LEFT OUTER JOIN keeps a row whose joined rows are all soft-deleted, with NULLs, as if the soft-deleted rows
did not exist: their filter goes into the join's ON clause, not the WHERE clause."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased, with_loader_criteria
from sqlalchemy.orm import join as orm_join

from tests.default_config.model import SDChild, SDChildChild, SDParent

DELETED_AT = datetime(2000, 1, 1)
ONLY_DELETED_CHILDREN = 2000
NO_CHILDREN = 2001
ONLY_DELETED_GRANDCHILDREN = 2002

parent_table = SDParent.__table__
child_table = SDChild.__table__
grandchild_table = SDChildChild.__table__

OUTER_JOINS: dict[str, Callable[[], Any]] = {
    "relationship": lambda: select(SDParent.id, SDChild.id).outerjoin(SDParent.children),
    "onclause": lambda: select(SDParent.id, SDChild.id).outerjoin(SDChild, SDChild.parent_id == SDParent.id),
    "implicit_onclause": lambda: select(SDParent.id, SDChild.id).outerjoin(SDChild),
    "relationship_onclause": lambda: select(SDParent.id, SDChild.id).outerjoin(SDChild, SDParent.children),
    "join_isouter": lambda: select(SDParent.id, SDChild.id).join(SDParent.children, isouter=True),
    "outerjoin_from": lambda: select(SDParent.id, SDChild.id).outerjoin_from(SDParent, SDChild),
    "relationship_with_criteria": lambda: select(SDParent.id, SDChild.id).outerjoin(
        SDParent.children.and_(SDChild.id > 0)
    ),
    "aliased_table_onclause": lambda: (
        lambda child: select(SDParent.id, child.id).outerjoin(child, child.parent_id == SDParent.id)
    )(aliased(SDChild)),
    "aliased_table_of_type": lambda: (
        lambda child: select(SDParent.id, child.id).outerjoin(SDParent.children.of_type(child))
    )(aliased(SDChild)),
    "set_aside_by_with_only_columns": lambda: (
        select(SDParent).outerjoin(SDParent.children).with_only_columns(SDParent.id, SDChild.id)
    ),
    "core_join_in_select_from": lambda: select(SDParent.id, SDChild.id).select_from(
        parent_table.outerjoin(child_table)
    ),
    "orm_join_in_select_from": lambda: select(SDParent.id, SDChild.id).select_from(
        orm_join(SDParent, SDChild, isouter=True)
    ),
}


@pytest.fixture
def outer_join_session(seeded_session: Session) -> Session:
    seeded_session.add_all([
        SDParent(id=ONLY_DELETED_CHILDREN),
        SDParent(id=NO_CHILDREN),
        SDChild(id=200000, parent_id=ONLY_DELETED_CHILDREN, deleted_at=DELETED_AT),
        SDChild(id=200001, parent_id=ONLY_DELETED_CHILDREN, deleted_at=DELETED_AT),
    ])
    seeded_session.commit()
    return seeded_session


def _expected_rows(session: Session) -> list[tuple[int, int | None]]:
    """Each active parent with each of its active children, or with None when it has none."""
    parents = session.scalars(
        select(SDParent.id).where(SDParent.deleted_at.is_(None)).execution_options(include_deleted=True)
    )
    rows: list[tuple[int, int | None]] = []
    for parent_id in parents:
        children = session.scalars(
            select(SDChild.id)
            .where(SDChild.parent_id == parent_id, SDChild.deleted_at.is_(None))
            .execution_options(include_deleted=True)
        ).all()
        rows.extend((parent_id, child_id) for child_id in children) if children else rows.append((parent_id, None))
    return sorted(rows, key=repr)


@pytest.mark.parametrize("make_stmt", OUTER_JOINS.values(), ids=OUTER_JOINS.keys())
def test_outer_join_keeps_rows_whose_joined_rows_are_all_soft_deleted(outer_join_session, make_stmt):
    expected = _expected_rows(outer_join_session)
    assert (ONLY_DELETED_CHILDREN, None) in expected

    for _ in range(2):
        rows = outer_join_session.execute(make_stmt()).all()

        assert sorted(rows, key=repr) == expected


def test_legacy_query_outer_join_keeps_rows_whose_joined_rows_are_all_soft_deleted(outer_join_session):
    rows = outer_join_session.query(SDParent.id, SDChild.id).outerjoin(SDParent.children).all()

    assert sorted(rows, key=repr) == _expected_rows(outer_join_session)


def test_inner_join_leaves_out_rows_whose_joined_rows_are_all_soft_deleted(outer_join_session):
    rows = outer_join_session.execute(select(SDParent.id, SDChild.id).join(SDParent.children)).all()

    assert sorted(rows, key=repr) == [row for row in _expected_rows(outer_join_session) if row[1] is not None]


FRESH_LEFT_SIDES: dict[str, Callable[[], Any]] = {
    "aliased_table": lambda: aliased(SDParent),
    "aliased_subquery": lambda: aliased(SDParent, select(SDParent).where(SDParent.id > 0).subquery()),
}


@pytest.mark.parametrize("make_left", FRESH_LEFT_SIDES.values(), ids=FRESH_LEFT_SIDES.keys())
def test_outer_join_without_an_onclause_from_a_new_alias_each_time(outer_join_session, make_left):
    """The ON clause SQLAlchemy finds refers to the statement's own alias; each statement gets its own."""
    expected = _expected_rows(outer_join_session)

    for _ in range(2):
        parent = make_left()
        rows = outer_join_session.execute(select(parent.id, SDChild.id).outerjoin_from(parent, SDChild)).all()

        assert sorted(rows, key=repr) == expected


def test_outer_join_anti_join_finds_parents_without_active_children(outer_join_session):
    stmt = select(SDParent.id).outerjoin(SDParent.children).where(SDChild.id.is_(None))

    ids = sorted(outer_join_session.scalars(stmt))

    assert ids == sorted(parent_id for parent_id, child_id in _expected_rows(outer_join_session) if child_id is None)
    assert ONLY_DELETED_CHILDREN in ids


def test_outer_join_counts_active_children(outer_join_session):
    stmt = select(SDParent.id, func.count(SDChild.id)).outerjoin(SDParent.children).group_by(SDParent.id)

    counts = dict(outer_join_session.execute(stmt).all())

    expected: dict[int, int] = {}
    for parent_id, child_id in _expected_rows(outer_join_session):
        expected[parent_id] = expected.get(parent_id, 0) + (child_id is not None)
    assert counts == expected
    assert counts[ONLY_DELETED_CHILDREN] == 0


def test_chain_of_outer_joins(outer_join_session):
    """Each outer join keeps its left rows, as an outer join of the active rows of each table would."""
    stmt = (
        select(SDParent.id, SDChild.id, SDChildChild.id).outerjoin(SDParent.children).outerjoin(SDChild.child_children)
    )

    rows = sorted(outer_join_session.execute(stmt).all(), key=repr)

    parents, children, grandchildren = (
        select(model).where(model.deleted_at.is_(None)).subquery() for model in (SDParent, SDChild, SDChildChild)
    )
    active_rows = (
        select(parents.c.id, children.c.id, grandchildren.c.id)
        .select_from(
            parents.outerjoin(children, children.c.parent_id == parents.c.id).outerjoin(
                grandchildren, grandchildren.c.child_id == children.c.id
            )
        )
        .execution_options(include_deleted=True)
    )
    assert rows == sorted(outer_join_session.execute(active_rows).all(), key=repr)


def test_inner_join_after_an_outer_join(outer_join_session):
    stmt = select(SDParent.id, SDChild.id, SDChildChild.id).outerjoin(SDParent.children).join(SDChild.child_children)

    rows = outer_join_session.execute(stmt).all()

    expected = outer_join_session.execute(
        select(SDParent.id, SDChild.id, SDChildChild.id)
        .join(SDParent.children)
        .join(SDChild.child_children)
        .where(SDParent.deleted_at.is_(None), SDChild.deleted_at.is_(None), SDChildChild.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    ).all()
    assert sorted(rows) == sorted(expected)


def test_full_outer_join_leaves_out_soft_deleted_rows(outer_join_session):
    """A FULL OUTER JOIN keeps the unmatched rows of both sides, so its filters stay in the WHERE clause."""
    stmt = select(SDParent.id, SDChild.id).outerjoin(SDChild, SDChild.parent_id == SDParent.id, full=True)

    rows = outer_join_session.execute(stmt).all()

    deleted_parents = set(
        outer_join_session.scalars(
            select(SDParent.id).where(SDParent.deleted_at.is_not(None)).execution_options(include_deleted=True)
        )
    )
    deleted_children = set(
        outer_join_session.scalars(
            select(SDChild.id).where(SDChild.deleted_at.is_not(None)).execution_options(include_deleted=True)
        )
    )
    assert rows
    assert not {parent_id for parent_id, _ in rows} & deleted_parents
    assert not {child_id for _, child_id in rows} & deleted_children


def _active_rows_of_chain(session: Session, inner_first_join: bool = False) -> list[Any]:
    """Each active parent, its active children and their active children, by outer joins of the active rows (an
    inner join to the children with ``inner_first_join``)."""
    parents, children, grandchildren = (
        select(model).where(model.deleted_at.is_(None)).subquery() for model in (SDParent, SDChild, SDChildChild)
    )
    joined = (parents.join if inner_first_join else parents.outerjoin)(children, children.c.parent_id == parents.c.id)
    stmt = (
        select(parents.c.id, children.c.id, grandchildren.c.id)
        .select_from(joined.outerjoin(grandchildren, grandchildren.c.child_id == children.c.id))
        .execution_options(include_deleted=True)
    )
    return sorted(session.execute(stmt).all(), key=repr)


@pytest.mark.parametrize("outer", [False, True], ids=["join", "outerjoin"])
def test_join_to_a_core_join_that_outer_joins(outer_join_session, outer):
    """The outer join nested in the joined target gets its own tables' filters; the join, those of the rest."""
    # An active child whose only child is soft-deleted.
    outer_join_session.add(SDParent(id=ONLY_DELETED_GRANDCHILDREN))
    outer_join_session.flush()
    outer_join_session.add(SDChild(id=ONLY_DELETED_GRANDCHILDREN * 100, parent_id=ONLY_DELETED_GRANDCHILDREN))
    outer_join_session.flush()
    outer_join_session.add(
        SDChildChild(
            id=ONLY_DELETED_GRANDCHILDREN * 1000, child_id=ONLY_DELETED_GRANDCHILDREN * 100, deleted_at=DELETED_AT
        )
    )
    outer_join_session.flush()
    assert (ONLY_DELETED_GRANDCHILDREN, ONLY_DELETED_GRANDCHILDREN * 100, None) in _active_rows_of_chain(
        outer_join_session, not outer
    )
    target = child_table.outerjoin(grandchild_table, grandchild_table.c.child_id == child_table.c.id)
    stmt = select(SDParent.id, child_table.c.id, grandchild_table.c.id)
    stmt = (stmt.outerjoin if outer else stmt.join)(target, child_table.c.parent_id == parent_table.c.id)

    for _ in range(2):
        rows = sorted(outer_join_session.execute(stmt).all(), key=repr)

        assert rows == _active_rows_of_chain(outer_join_session, inner_first_join=not outer)


def test_outer_join_without_an_onclause_keeps_the_statements_loader_criteria(outer_join_session):
    expected = _expected_rows(outer_join_session)
    left_out = next(child_id for _, child_id in expected if child_id is not None)
    stmt = (
        select(SDParent.id, SDChild.id)
        .outerjoin(SDChild)
        .options(with_loader_criteria(SDChild, SDChild.id != left_out))
    )

    rows = sorted(outer_join_session.execute(stmt).all(), key=repr)

    assert left_out not in {child_id for _, child_id in rows}
    kept = [row for row in expected if row[1] != left_out]
    parents = {parent_id for parent_id, _ in expected}
    orphaned = parents - {parent_id for parent_id, _ in kept}
    assert rows == sorted(kept + [(parent_id, None) for parent_id in orphaned], key=repr)
