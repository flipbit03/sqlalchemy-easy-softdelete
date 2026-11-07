"""Soft-delete conditions added to the ON clauses of a statement's joins: those of soft-deletable secondaries, and
those of the tables on the right of its LEFT OUTER JOINs."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import and_
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql import Join, Select, visitors
from sqlalchemy.sql.selectable import FromClause

from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals
from sqlalchemy_easy_softdelete.handler.rewriter.sa.clauses import (
    join_leaves,
    join_target_selectable,
    joins_of,
    plain,
    ungrouped,
)
from sqlalchemy_easy_softdelete.handler.rewriter.transform.copies import on_copies


def with_secondary_criteria(stmt: Select[Any], criteria: tuple[tuple[int, Any], ...]) -> Any:
    """``stmt`` with the soft-delete condition of each secondary table in ``criteria`` added to its join along
    the relationship (``relationship.and_()``). Joins are numbered as ``joins_of()`` lists them: the
    statement's own, then those ``with_only_columns()`` set aside."""
    conditions = dict(criteria)

    def add(stmt_joins: tuple[Any, ...], offset: int) -> tuple[Any, ...]:
        rewritten = []
        for position, (target, onclause, from_, flags) in enumerate(stmt_joins, start=offset):
            condition = conditions.get(position)
            if condition is not None:
                if isinstance(onclause, QueryableAttribute):
                    onclause = onclause.and_(condition)
                else:
                    target = cast(QueryableAttribute[Any], target).and_(condition)
            rewritten.append((target, onclause, from_, flags))
        return tuple(rewritten)

    own_joins = internals.joins(stmt)
    parts: dict[str, Any] = {"joins": add(own_joins, 0)}
    offset = len(own_joins)
    set_aside = internals.set_aside_entities(stmt)
    if set_aside:
        memoized = []
        for entities in set_aside:
            entity_joins = internals.joins(entities)
            new_joins = add(entity_joins, offset)
            offset += len(entity_joins)
            if any(position in conditions for position in range(offset - len(new_joins), offset)):
                entities = internals.with_joins(entities, new_joins)
            memoized.append(entities)
        parts["set_aside_entities"] = tuple(memoized)
    return internals.copy_statement(stmt, **parts)


def with_outer_join_filters(
    stmt: Select[Any], outer_joins: tuple[tuple[Any, Any], ...], filters: list[Any], copies: dict[int, FromClause]
) -> tuple[Any, list[Any]]:
    """``stmt`` with the soft-delete columns of ``filters`` that belong to the right side of one of its
    ``outer_joins`` filtered in that join's ON clause, and the columns left for the WHERE clause.

    The right sides are read from ``stmt`` itself, whose aliases of tables are its own. The outer joins nested in
    a join come first, so that each column goes to the innermost one. A join that gives no ON clause gets the
    one SQLAlchemy found, on the copies of the subqueries it refers to (``copies``).
    """
    remaining = {id(column): column for column in filters}

    def take(selectable: Any) -> list[Any]:
        leaves = {id(plain(leaf)) for leaf in join_leaves(selectable)}
        moved = [column for column in remaining.values() if id(plain(column.table)) in leaves]
        for column in moved:
            del remaining[id(column)]
        return [column.is_(None) for column in moved]

    from_objs = list(internals.from_list(stmt))
    all_joins = list(joins_of(stmt))
    for position, computed in outer_joins:
        if isinstance(position, tuple):
            kind, index, path = position
            tree = from_objs[index] if kind == "from" else all_joins[index][0]
            join = _join_at(tree, path)
            conditions = take(join.right)
            if conditions:
                tree = _with_on_conditions(tree, join, conditions)
                if kind == "from":
                    from_objs[index] = tree
                else:
                    all_joins[index] = (tree, *all_joins[index][1:])
        else:
            conditions = take(join_target_selectable(all_joins[position][0]))
            if conditions:
                computed = on_copies(computed, copies) if computed is not None else None
                all_joins[position] = _join_with_conditions(all_joins[position], conditions, computed)

    offset = len(internals.joins(stmt))
    parts: dict[str, Any] = {"from_list": tuple(from_objs), "joins": tuple(all_joins[:offset])}
    set_aside = internals.set_aside_entities(stmt)
    if set_aside:
        memoized = []
        for entities in set_aside:
            entity_joins = internals.joins(entities)
            joins_set_aside = tuple(all_joins[offset : offset + len(entity_joins)])
            offset += len(entity_joins)
            if any(new is not old for new, old in zip(joins_set_aside, entity_joins)):
                entities = internals.with_joins(entities, joins_set_aside)
            memoized.append(entities)
        parts["set_aside_entities"] = tuple(memoized)
    return internals.copy_statement(stmt, **parts), list(remaining.values())


def _join_at(from_obj: Any, path: tuple[str, ...]) -> Join:
    for step in path:
        from_obj = ungrouped(from_obj)
        from_obj = from_obj.left if step == "l" else from_obj.right
    return cast(Join, ungrouped(from_obj))


def _join_with_conditions(join: tuple[Any, ...], conditions: list[Any], computed: Any) -> tuple[Any, ...]:
    """A join of a statement (a ``joins()`` entry) with ``conditions`` added to its ON clause: to the
    relationship it joins along, or to its ON clause (``computed``, for a join that gives none)."""
    target, onclause, from_, flags = join
    if isinstance(target, QueryableAttribute) and onclause is None:
        return (target.and_(*conditions), onclause, from_, flags)
    if isinstance(onclause, QueryableAttribute):
        return (target, onclause.and_(*conditions), from_, flags)
    return (target, and_(cast(Any, onclause if onclause is not None else computed), *conditions), from_, flags)


def _with_on_conditions(from_obj: Any, join: Join, conditions: list[Any]) -> Any:
    """``from_obj``, a join tree written with Core, with ``conditions`` added to the ON clause of ``join``."""
    replacement = internals.clone(join)
    replacement.onclause = and_(cast(Any, join.onclause), *conditions)

    def replace(element: Any, **kw: Any) -> Any:
        return replacement if element is join else None

    return visitors.replacement_traverse(from_obj, {}, replace)
