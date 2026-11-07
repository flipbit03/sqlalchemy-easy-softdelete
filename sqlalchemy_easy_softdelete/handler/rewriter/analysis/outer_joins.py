"""The LEFT OUTER JOINs of a statement whose right side holds soft-deletable tables: those are filtered in the
join's ON clause, so that the left rows stay (see ``SelectPlan.outer_joins``)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, Table
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql import Alias, Join, Select

from sqlalchemy_easy_softdelete.handler.rewriter.analysis.froms import joins_by_right
from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals
from sqlalchemy_easy_softdelete.handler.rewriter.sa.clauses import (
    join_leaves,
    join_target_selectable,
    joins_of,
    plain,
    ungrouped,
)


def outer_joins(
    stmt: Select[Any], final_froms: Any, columns: list[Column[Any]], table_aliases: list[Alias]
) -> tuple[tuple[Any, Any], ...]:
    """See ``SelectPlan.outer_joins``."""
    filtered = {id(column.table) for column in columns} | {id(plain(alias)) for alias in table_aliases}
    if not filtered:
        return ()

    def holds_filtered(selectable: Any) -> bool:
        return any(id(plain(leaf)) in filtered for leaf in join_leaves(selectable))

    found: list[tuple[Any, Any]] = []
    for index, from_obj in enumerate(internals.from_list(stmt)):
        for path, join in outer_joins_in(from_obj):
            if holds_filtered(join.right):
                found.append((("from", index, path), None))
    by_right: dict[int, Join] | None = None
    for position, (target, onclause, _from, flags) in enumerate(joins_of(stmt)):
        # A join to a join written with Core: its own outer joins come first.
        for path, join in outer_joins_in(target):
            if holds_filtered(join.right):
                found.append((("target", position, path), None))
        if not flags.get("isouter") or flags.get("full"):
            continue
        selectable = join_target_selectable(target)
        if selectable is None or not holds_filtered(selectable):
            continue
        computed = None
        if onclause is None and not isinstance(target, QueryableAttribute):
            if by_right is None:
                by_right = joins_by_right(final_froms)
            joined = by_right.get(id(plain(ungrouped(selectable))))
            if joined is None or joined.onclause is None:
                continue
            computed = joined.onclause
        found.append((position, computed))
    return tuple(found)


def outer_joins_in(from_obj: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Join]]:
    """The LEFT OUTER JOINs of a join tree written with Core (or ``orm.join()``), each with its path from the
    root (``l``/``r`` steps)."""
    from_obj = ungrouped(from_obj)
    if not isinstance(from_obj, Join):
        return []
    found = outer_joins_in(from_obj.left, path + ("l",)) + outer_joins_in(from_obj.right, path + ("r",))
    if from_obj.isouter and not from_obj.full:
        found.append((path, from_obj))
    return found


def refers_to_statement_objects(onclause: Any) -> bool:
    """Whether ``onclause`` refers to a FROM other than a table (an alias, a subquery): one of its statement's."""
    if onclause is None:
        return False
    return any(not isinstance(from_obj, Table) for from_obj in internals.from_objects(onclause))
