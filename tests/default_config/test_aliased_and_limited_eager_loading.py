"""Soft-delete filtering of aliased() entities, and of eager loading around a LIMIT.

- A subquery reached through ``aliased(Model, subquery)`` is filtered without modifying it, wherever
  the entity is referred to (loader options, Bundles, unions, joins), and rows stay keyed by it.
- Joining an ``aliased()`` entity explicitly, while eager loading the same entity, renders valid SQL.
- With eager loading and a LIMIT, the ORM moves the statement into a subquery; the tables of the
  statement are still filtered.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import false, func, select, union_all
from sqlalchemy.orm import (
    Bundle,
    Load,
    Session,
    aliased,
    contains_eager,
    defer,
    joinedload,
    load_only,
    selectinload,
    subqueryload,
    with_polymorphic,
)

from tests.default_config.model import SDBaseRequest, SDChild, SDChildChild, SDDerivedRequest, SDParent

DELETED_PARENT_ID = 1002


def _active_child_ids(session: Session) -> list[int]:
    stmt = select(SDChild.id).where(SDChild.deleted_at.is_(None)).execution_options(include_deleted=True)
    return sorted(session.scalars(stmt))


def _active_child_ids_of_active_parents(session: Session) -> list[int]:
    stmt = (
        select(SDChild.id)
        .join(SDChild.parent)
        .where(SDChild.deleted_at.is_(None), SDParent.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    return sorted(session.scalars(stmt))


# Each case: (builds the aliased() entity, the expected child ids)
ALIASED_SUBQUERIES: dict[str, tuple[Callable[[], Any], Callable[[Session], list[int]]]] = {
    "orm_select": (lambda: aliased(SDChild, select(SDChild).subquery()), _active_child_ids),
    "core_select": (lambda: aliased(SDChild, select(SDChild.__table__).subquery()), _active_child_ids),
    "select_with_join": (
        lambda: aliased(SDChild, select(SDChild).join(SDChild.parent).subquery()),
        _active_child_ids_of_active_parents,
    ),
}


@pytest.mark.parametrize("make_alias, expected", ALIASED_SUBQUERIES.values(), ids=ALIASED_SUBQUERIES.keys())
def test_aliased_subquery_entity_is_filtered(seeded_session, make_alias, expected):
    alias = make_alias()

    ids = sorted(child.id for child in seeded_session.scalars(select(alias)))

    assert ids == expected(seeded_session)


def test_aliased_subquery_joined_with_onclause_is_filtered(seeded_session):
    alias = aliased(SDChild, select(SDChild).subquery())
    stmt = select(alias.id).join(SDParent, alias.parent_id == SDParent.id)

    assert sorted(seeded_session.scalars(stmt)) == _active_child_ids_of_active_parents(seeded_session)


def test_aliased_subquery_joined_through_relationship_is_filtered(seeded_session):
    alias = aliased(SDChild, select(SDChild).subquery())
    stmt = select(alias.id).select_from(SDParent).join(SDParent.children.of_type(alias))

    assert sorted(seeded_session.scalars(stmt)) == _active_child_ids_of_active_parents(seeded_session)


def test_include_deleted_inside_an_aliased_subquery_is_honored(seeded_session):
    alias = aliased(SDChild, select(SDChild).execution_options(include_deleted=True).subquery())

    ids = sorted(child.id for child in seeded_session.scalars(select(alias)))

    all_child_ids = sorted(seeded_session.scalars(select(SDChild.id).execution_options(include_deleted=True)))
    assert ids == all_child_ids


@pytest.mark.parametrize(
    "make_alias",
    [lambda: aliased(SDChild), lambda: aliased(SDChild, select(SDChild).subquery())],
    ids=["aliased_table", "aliased_subquery"],
)
def test_explicit_join_to_aliased_entity_with_eager_load_of_that_entity(seeded_session, make_alias):
    alias = make_alias()
    stmt = select(SDParent).join(alias, alias.parent_id == SDParent.id).options(joinedload(SDParent.children))

    parents = seeded_session.scalars(stmt).unique().all()

    assert parents
    assert all(parent.deleted_at is None for parent in parents)
    assert all(child.deleted_at is None for parent in parents for child in parent.children)


def test_eager_load_with_limit_filters_the_tables_joined_in_the_statement(seeded_session):
    stmt = select(SDChild).join(SDChild.parent).options(joinedload(SDChild.child_children)).limit(1000)

    children = seeded_session.scalars(stmt).unique().all()

    assert sorted(child.id for child in children) == _active_child_ids_of_active_parents(seeded_session)
    assert all(grandchild.deleted_at is None for child in children for grandchild in child.child_children)


def test_eager_load_with_limit_filters_the_queried_entity(seeded_session):
    stmt = select(SDParent).options(joinedload(SDParent.children)).order_by(SDParent.id.desc()).limit(1)

    parent = seeded_session.scalars(stmt).unique().one()

    assert parent.id != DELETED_PARENT_ID
    assert all(child.deleted_at is None for child in parent.children)


def test_query_first_with_eager_load_filters_the_queried_entity(seeded_session):
    parent = seeded_session.query(SDParent).options(joinedload(SDParent.children)).order_by(SDParent.id.desc()).first()

    assert parent is not None
    assert parent.id != DELETED_PARENT_ID


def test_eager_load_with_limit_of_a_child_entity_filters_grandchildren(seeded_session):
    stmt = select(SDChildChild).join(SDChildChild.child).options(joinedload(SDChildChild.child)).limit(1000)

    grandchildren = seeded_session.scalars(stmt).unique().all()

    assert grandchildren
    assert all(grandchild.deleted_at is None and grandchild.child.deleted_at is None for grandchild in grandchildren)


def _aliased_active_children() -> Any:
    return aliased(SDChild, select(SDChild).subquery())


NESTED_ALIASED_STATEMENTS: dict[str, Callable[[Session], Any]] = {
    "count_wrapper": lambda s: s.scalar(
        select(func.count()).select_from(select(_aliased_active_children()).subquery())
    ),
    "query_count": lambda s: s.query(_aliased_active_children()).count(),
    "union_member": lambda s: len(
        s.execute(union_all(select(_aliased_active_children().id), select(SDChild.id).where(false()))).all()
    ),
    "select_from_nested_select": lambda s: len(
        s.execute(select(select(_aliased_active_children().id).subquery())).all()
    ),
}


@pytest.mark.parametrize("count", NESTED_ALIASED_STATEMENTS.values(), ids=NESTED_ALIASED_STATEMENTS.keys())
def test_aliased_subquery_statement_nested_in_another_select_is_filtered(seeded_session, count):
    assert count(seeded_session) == len(_active_child_ids(seeded_session))


def test_core_tables_inside_an_aliased_subquery_are_filtered(seeded_session):
    child, parent = SDChild.__table__, SDParent.__table__
    inner = select(child).join(parent, parent.c.id == child.c.parent_id)
    alias = aliased(SDChild, inner.subquery())

    ids = sorted(row.id for row in seeded_session.scalars(select(alias)))

    assert ids == _active_child_ids_of_active_parents(seeded_session)


def test_union_with_core_member_inside_an_aliased_subquery_is_filtered(seeded_session):
    child = SDChild.__table__
    inner = union_all(select(SDChild).where(SDChild.parent_id == 1000), select(child).where(child.c.parent_id == 1001))
    alias = aliased(SDChild, inner.subquery())

    ids = sorted(row.id for row in seeded_session.scalars(select(alias)))

    expected = [i for i in _active_child_ids(seeded_session) if i // 100 in (1000, 1001)]
    assert ids == expected


def test_include_deleted_nested_inside_an_aliased_subquery_is_honored(seeded_session):
    child = SDChild.__table__
    everything = select(child).execution_options(include_deleted=True).subquery()
    alias = aliased(SDChild, select(everything).subquery())

    ids = sorted(row.id for row in seeded_session.scalars(select(alias)))

    all_child_ids = sorted(seeded_session.scalars(select(SDChild.id).execution_options(include_deleted=True)))
    assert ids == all_child_ids


def test_aliased_subquery_does_not_filter_other_selects_that_include_deleted(seeded_session):
    """The filter of an aliased() subquery stays inside it: a SELECT next to it that includes
    soft-deleted rows still includes them."""
    all_children = (
        select(SDChild.parent_id, func.count().label("n"))
        .group_by(SDChild.parent_id)
        .execution_options(include_deleted=True)
        .subquery()
    )
    alias = _aliased_active_children()
    stmt = (
        select(all_children.c.parent_id, all_children.c.n)
        .join(alias, alias.parent_id == all_children.c.parent_id)
        .distinct()
    )

    counts = sorted(seeded_session.execute(stmt).all())

    totals = sorted(
        seeded_session.execute(
            select(SDChild.parent_id, func.count()).group_by(SDChild.parent_id).execution_options(include_deleted=True)
        ).all()
    )
    assert counts == totals


def test_with_only_columns_after_a_join_from_an_aliased_subquery_is_filtered(seeded_session):
    alias = _aliased_active_children()
    joined = select(alias).join(alias.parent)

    ids = sorted(seeded_session.scalars(joined.with_only_columns(alias.id)))
    count = seeded_session.scalar(joined.with_only_columns(func.count()))

    assert ids == _active_child_ids_of_active_parents(seeded_session)
    assert count == len(ids)


def test_query_with_entities_after_a_join_from_an_aliased_subquery_is_filtered(seeded_session):
    alias = _aliased_active_children()

    ids = sorted(row.id for row in seeded_session.query(alias).join(alias.parent).with_entities(alias.id))

    assert ids == _active_child_ids_of_active_parents(seeded_session)


def test_with_only_columns_after_a_loader_option_on_an_aliased_subquery_is_filtered(seeded_session):
    alias = _aliased_active_children()
    stmt = select(alias).options(selectinload(alias.child_children)).with_only_columns(func.count(alias.id))

    assert seeded_session.scalar(stmt) == len(_active_child_ids(seeded_session))


@pytest.mark.parametrize(
    "make_alias",
    [
        lambda: aliased(_aliased_active_children()),
        lambda: aliased(SDChild, select(SDChild).subquery().alias()),
    ],
    ids=["alias_of_aliased_entity", "aliased_over_subquery_alias"],
)
def test_alias_of_an_aliased_subquery_is_filtered(seeded_session, make_alias):
    alias = make_alias()

    ids = sorted(seeded_session.scalars(select(alias.id)))

    assert ids == _active_child_ids(seeded_session)


@pytest.mark.parametrize("loader", [joinedload, selectinload, subqueryload], ids=lambda loader: loader.__name__)
def test_eager_load_into_an_aliased_subquery_is_filtered(seeded_session, loader):
    alias = _aliased_active_children()
    stmt = select(SDParent).options(loader(SDParent.children.of_type(alias)))

    parents = seeded_session.scalars(stmt).unique().all()

    assert parents
    assert all(child.deleted_at is None for parent in parents for child in parent.children)


def test_bundle_of_aliased_subquery_columns_is_filtered(seeded_session):
    alias = _aliased_active_children()

    rows = seeded_session.scalars(select(Bundle("child", alias.id, alias.parent_id))).all()

    assert sorted(row.id for row in rows) == _active_child_ids(seeded_session)


def test_include_deleted_inside_an_aliased_subquery_with_a_loader_option_is_honored(seeded_session):
    everything = select(SDChild).execution_options(include_deleted=True).subquery()
    alias = aliased(SDChild, select(everything).subquery())
    stmt = select(alias).options(selectinload(alias.child_children))

    ids = sorted(child.id for child in seeded_session.scalars(stmt))

    assert ids == sorted(seeded_session.scalars(select(SDChild.id).execution_options(include_deleted=True)))


def _child_totals_including_deleted(session: Session) -> dict[int, int]:
    stmt = select(SDChild.parent_id, func.count()).group_by(SDChild.parent_id).execution_options(include_deleted=True)
    return {parent_id: count for parent_id, count in session.execute(stmt).all()}


@pytest.mark.parametrize(
    "option",
    [lambda a: load_only(a.parent_id), lambda a: defer(a.parent_id), lambda a: selectinload(a.child_children)],
    ids=["load_only", "defer", "selectinload"],
)
def test_loader_option_on_an_aliased_subquery_entity_is_filtered(seeded_session, option):
    alias = _aliased_active_children()

    ids = sorted(child.id for child in seeded_session.scalars(select(alias).options(option(alias))))

    assert ids == _active_child_ids(seeded_session)


def test_loader_option_on_an_aliased_subquery_does_not_filter_a_subquery_that_includes_deleted(seeded_session):
    counts = (
        select(SDChild.parent_id, func.count().label("n"))
        .group_by(SDChild.parent_id)
        .execution_options(include_deleted=True)
        .subquery()
    )
    alias = _aliased_active_children()
    stmt = (
        select(alias, counts.c.n)
        .join(counts, counts.c.parent_id == alias.parent_id)
        .options(selectinload(alias.child_children))
    )

    rows = seeded_session.execute(stmt).all()

    totals = _child_totals_including_deleted(seeded_session)
    assert sorted(child.id for child, _ in rows) == _active_child_ids(seeded_session)
    assert all(count == totals[child.parent_id] for child, count in rows)


def test_loader_option_on_an_aliased_subquery_does_not_filter_an_alias_that_includes_deleted(seeded_session):
    alias = _aliased_active_children()
    everything = aliased(SDChild, select(SDChild).execution_options(include_deleted=True).subquery())
    stmt = (
        select(alias, func.count(everything.id))
        .join(everything, everything.parent_id == alias.parent_id)
        .group_by(alias.id, alias.parent_id)
        .options(load_only(alias.parent_id))
    )

    rows = seeded_session.execute(stmt).all()

    totals = _child_totals_including_deleted(seeded_session)
    assert sorted(child.id for child, _ in rows) == _active_child_ids(seeded_session)
    assert all(count == totals[child.parent_id] for child, count in rows)


def test_loader_option_on_an_aliased_subquery_with_a_limit_filters_inside_the_subquery(seeded_session):
    alias = aliased(SDChild, select(SDChild).order_by(SDChild.id.desc()).limit(3).subquery())
    stmt = select(alias).options(selectinload(alias.child_children))

    ids = sorted(child.id for child in seeded_session.scalars(stmt))

    assert ids == _active_child_ids(seeded_session)[-3:]


def _active_request_ids(session: Session) -> list[int]:
    stmt = select(SDBaseRequest.id).where(SDBaseRequest.deleted_at.is_(None)).execution_options(include_deleted=True)
    return sorted(session.scalars(stmt))


def _union_with(column: Any) -> Any:
    return union_all(select(column), select(SDChild.id).where(false()))


# Each case: (the ids the union selects, the expected ids)
UNIONS_OF_ALIASED_SUBQUERIES: dict[str, tuple[Callable[[Session], list[int]], Callable[[Session], list[int]]]] = {
    "with_polymorphic": (
        lambda s: sorted(
            row[0]
            for row in s.execute(_union_with(with_polymorphic(SDBaseRequest, [SDDerivedRequest], aliased=True).id))
        ),
        _active_request_ids,
    ),
    "with_polymorphic_in_a_subquery": (
        lambda s: sorted(
            s.scalars(
                select(
                    _union_with(with_polymorphic(SDBaseRequest, [SDDerivedRequest], aliased=True).id).subquery().c.id
                )
            )
        ),
        _active_request_ids,
    ),
    "alias_of_an_aliased_subquery_with_a_limit": (
        lambda s: sorted(
            row[0]
            for row in s.execute(
                _union_with(
                    aliased(aliased(SDChild, select(SDChild).order_by(SDChild.id.desc()).limit(3).subquery())).id
                )
            )
        ),
        lambda s: _active_child_ids(s)[-3:],
    ),
}


@pytest.mark.parametrize(
    "ids, expected", UNIONS_OF_ALIASED_SUBQUERIES.values(), ids=UNIONS_OF_ALIASED_SUBQUERIES.keys()
)
def test_union_member_selecting_from_an_aliased_subquery_is_filtered(seeded_session, ids, expected):
    assert ids(seeded_session) == expected(seeded_session)


@pytest.mark.parametrize("loader", [joinedload, selectinload], ids=lambda loader: loader.__name__)
def test_eager_load_does_not_filter_an_aliased_core_subquery_that_includes_deleted(seeded_session, loader):
    everything = aliased(SDChild, select(SDChild.__table__).execution_options(include_deleted=True).subquery())
    stmt = (
        select(SDParent, func.count(everything.id))
        .join(everything, everything.parent_id == SDParent.id)
        .group_by(SDParent.id)
        .options(loader(SDParent.children))
    )

    rows = seeded_session.execute(stmt).unique().all()

    totals = _child_totals_including_deleted(seeded_session)
    assert sorted((parent.id, count) for parent, count in rows) == sorted(
        (parent_id, count) for parent_id, count in totals.items() if parent_id != DELETED_PARENT_ID
    )
    assert all(child.deleted_at is None for parent, _ in rows for child in parent.children)


@pytest.mark.parametrize(
    "make_alias",
    [
        lambda: aliased(_aliased_active_children()),
        lambda: aliased(SDChild, select(SDChild).subquery().alias()),
    ],
    ids=["alias_of_aliased_entity", "aliased_over_subquery_alias"],
)
def test_join_to_an_alias_of_an_aliased_subquery_is_filtered(seeded_session, make_alias):
    alias = make_alias()
    stmt = select(alias.id).select_from(SDParent).join(alias, alias.parent_id == SDParent.id)

    assert sorted(seeded_session.scalars(stmt)) == _active_child_ids_of_active_parents(seeded_session)


def test_join_to_an_alias_of_a_subquery_is_filtered(seeded_session):
    children = select(SDChild).subquery().alias("children")
    stmt = select(children.c.id).select_from(SDParent).join(children, children.c.parent_id == SDParent.id)

    assert sorted(seeded_session.scalars(stmt)) == _active_child_ids_of_active_parents(seeded_session)


def _add_parent_whose_children_are_all_deleted(session: Session) -> int:
    parent_id = 1003
    session.add(SDParent(id=parent_id))
    session.flush()
    session.add(SDChild(id=parent_id * 100, parent_id=parent_id, deleted_at=datetime(2000, 1, 1)))
    session.flush()
    return parent_id


OUTER_JOINS_TO_AN_ALIASED_SUBQUERY: dict[str, Callable[[Any], Any]] = {
    "outerjoin_with_load_only": lambda alias: (
        select(SDParent, alias)
        .outerjoin(alias, alias.parent_id == SDParent.id)
        .options(Load(alias).load_only(alias.parent_id))
    ),
    "relationship_outerjoin_with_contains_eager": lambda alias: (
        select(SDParent)
        .outerjoin(SDParent.children.of_type(alias))
        .options(contains_eager(SDParent.children.of_type(alias)))
    ),
}


@pytest.mark.parametrize(
    "make_stmt", OUTER_JOINS_TO_AN_ALIASED_SUBQUERY.values(), ids=OUTER_JOINS_TO_AN_ALIASED_SUBQUERY.keys()
)
def test_outer_join_to_an_aliased_subquery_keeps_rows_without_an_active_match(seeded_session, make_stmt):
    """Filtering the subquery's rows must not drop the rows that the LEFT OUTER JOIN keeps."""
    lonely_parent_id = _add_parent_whose_children_are_all_deleted(seeded_session)
    seeded_session.expunge_all()

    rows = seeded_session.execute(make_stmt(_aliased_active_children())).unique().all()

    active_parent_ids = sorted(
        seeded_session.scalars(
            select(SDParent.id).where(SDParent.deleted_at.is_(None)).execution_options(include_deleted=True)
        )
    )
    assert lonely_parent_id in active_parent_ids
    assert sorted({row[0].id for row in rows}) == active_parent_ids
    loaded_children = [obj for obj in seeded_session.identity_map.values() if isinstance(obj, SDChild)]
    assert loaded_children
    assert all(child.deleted_at is None for child in loaded_children)


def _child_ids_of_parents(session: Session, *parent_ids: int) -> list[int]:
    return [child_id for child_id in _active_child_ids(session) if child_id // 100 in parent_ids]


def _core_join_of_children_and_parents() -> Any:
    child, parent = SDChild.__table__, SDParent.__table__
    return select(child).join(parent, parent.c.id == child.c.parent_id)


def _orm_select_joined_to_a_core_table() -> Any:
    parent = SDParent.__table__
    return select(SDChild).join(parent, parent.c.id == SDChild.parent_id)


def _union_with_a_core_member() -> Any:
    child = SDChild.__table__
    return union_all(select(SDChild).where(SDChild.parent_id == 1000), select(child).where(child.c.parent_id == 1001))


# Each case: (builds the statement inside the subquery, the expected child ids)
STATEMENTS_WITH_CORE_TABLES: dict[str, tuple[Callable[[], Any], Callable[[Session], list[int]]]] = {
    "core_join": (_core_join_of_children_and_parents, _active_child_ids_of_active_parents),
    "orm_select_joined_to_a_core_table": (_orm_select_joined_to_a_core_table, _active_child_ids_of_active_parents),
    "union_with_a_core_member": (_union_with_a_core_member, lambda s: _child_ids_of_parents(s, 1000, 1001)),
}

# Each case: the statement selecting from the aliased() entity, which it refers to in other ways too
REFERENCES_TO_AN_ALIASED_ENTITY: dict[str, Callable[[Any], Any]] = {
    "load_only": lambda alias: select(alias).options(load_only(alias.parent_id)),
    "bundle": lambda alias: select(Bundle("child", alias.id, alias.parent_id)),
    "alias_of_it": lambda alias: select(aliased(alias)),
}


@pytest.mark.parametrize(
    "make_stmt", REFERENCES_TO_AN_ALIASED_ENTITY.values(), ids=REFERENCES_TO_AN_ALIASED_ENTITY.keys()
)
@pytest.mark.parametrize(
    "make_inner, expected", STATEMENTS_WITH_CORE_TABLES.values(), ids=STATEMENTS_WITH_CORE_TABLES.keys()
)
def test_core_tables_inside_an_aliased_subquery_are_filtered_wherever_it_is_referred_to(
    seeded_session, make_inner, expected, make_stmt
):
    alias = aliased(SDChild, make_inner().subquery())

    ids = sorted(row[0].id for row in seeded_session.execute(make_stmt(alias)))

    assert ids == expected(seeded_session)


def test_bundle_of_subquery_columns_is_filtered(seeded_session):
    children = select(SDChild).subquery()

    rows = seeded_session.execute(select(Bundle("child", children.c.id, children.c.parent_id))).all()

    assert sorted(row[0].id for row in rows) == _active_child_ids(seeded_session)


def test_bundle_of_joined_subquery_columns_is_filtered(seeded_session):
    children = select(SDChild).subquery()
    stmt = select(Bundle("child", children.c.id)).join(SDParent, SDParent.id == children.c.parent_id)

    rows = seeded_session.execute(stmt).all()

    assert sorted(row[0].id for row in rows) == _active_child_ids_of_active_parents(seeded_session)


def _aliased_children_with_a_limit() -> Any:
    return aliased(SDChild, select(SDChild).order_by(SDChild.id).limit(1000).subquery())


def test_loader_option_on_an_aliased_subquery_with_a_limit_does_not_filter_a_subquery_that_includes_deleted(
    seeded_session,
):
    counts = (
        select(SDChild.parent_id, func.count().label("n"))
        .group_by(SDChild.parent_id)
        .execution_options(include_deleted=True)
        .subquery()
    )
    alias = _aliased_children_with_a_limit()
    stmt = (
        select(alias, counts.c.n)
        .join(counts, counts.c.parent_id == alias.parent_id)
        .options(selectinload(alias.child_children))
    )

    rows = seeded_session.execute(stmt).all()

    totals = _child_totals_including_deleted(seeded_session)
    assert sorted(child.id for child, _ in rows) == _active_child_ids(seeded_session)
    assert all(count == totals[child.parent_id] for child, count in rows)


def test_union_member_that_includes_deleted_after_an_aliased_subquery_with_a_limit(seeded_session):
    alias = _aliased_children_with_a_limit()
    union = union_all(select(aliased(alias).id), select(SDChild.id).execution_options(include_deleted=True))

    ids = sorted(row[0] for row in seeded_session.execute(union))

    all_child_ids = list(seeded_session.scalars(select(SDChild.id).execution_options(include_deleted=True)))
    assert ids == sorted(_active_child_ids(seeded_session) + all_child_ids)


@pytest.mark.parametrize("loader", [joinedload, selectinload, subqueryload], ids=lambda loader: loader.__name__)
def test_eager_load_into_an_aliased_subquery_that_includes_deleted_loads_them(seeded_session, loader):
    everything = aliased(SDChild, select(SDChild).execution_options(include_deleted=True).subquery())

    parents = seeded_session.scalars(select(SDParent).options(loader(SDParent.children.of_type(everything)))).unique()

    expected = seeded_session.scalars(
        select(SDChild.id)
        .join(SDChild.parent)
        .where(SDParent.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    assert sorted(child.id for parent in parents for child in parent.children) == sorted(expected)


def _active_children_with_their_active_parent(session: Session) -> list[tuple[int, int | None]]:
    """Each active child with its parent's id, or None when the parent is soft-deleted."""
    stmt = (
        select(SDChild.id, SDParent.id, SDParent.deleted_at)
        .outerjoin(SDChild.parent)
        .where(SDChild.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    rows = [
        (child_id, None if deleted_at is not None else parent_id)
        for child_id, parent_id, deleted_at in session.execute(stmt)
    ]
    return sorted(rows, key=repr)


@pytest.mark.parametrize("outer", [False, True], ids=["join", "outerjoin"])
def test_join_from_an_aliased_subquery_along_its_relationship(seeded_session, outer):
    alias = _aliased_active_children()
    stmt = select(alias.id, SDParent.id)
    join_from = stmt.outerjoin_from if outer else stmt.join_from

    rows = sorted(seeded_session.execute(join_from(alias, alias.parent)).all(), key=repr)

    # The soft-deleted parent is never joined: a join drops its children, an outer join keeps them with NULLs, as
    # if it did not exist (its filter is in the ON clause).
    expected = _active_children_with_their_active_parent(seeded_session)
    assert any(parent_id is None for _, parent_id in expected)
    assert rows == (expected if outer else [row for row in expected if row[1] is not None])


def test_eager_load_next_to_an_aliased_subquery_without_the_soft_delete_column(seeded_session):
    """The eager loading criteria leave out an aliased() entity whose subquery has no soft-delete column,
    which they would filter on the class's own table, adding that table to the FROM clause (one row per
    row of the table)."""
    ids = aliased(SDChild, select(SDChild.id, SDChild.parent_id).subquery())
    stmt = select(SDParent, ids.id).join(ids, ids.parent_id == SDParent.id).options(selectinload(SDParent.children))

    rows = seeded_session.execute(stmt).all()

    assert sorted(child_id for _, child_id in rows) == _active_child_ids_of_active_parents(seeded_session)
    assert all(child.deleted_at is None for parent, _ in rows for child in parent.children)
