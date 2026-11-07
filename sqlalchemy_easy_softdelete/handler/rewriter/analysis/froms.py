"""The FROM clause of a statement: its soft-deletable tables, its subqueries and its aliases of tables."""

from __future__ import annotations

import itertools
from typing import Any

from sqlalchemy import Column, Table
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql import Alias, Join, Select, TableClause
from sqlalchemy.sql import util as sql_util
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.selectable import Exists, FromClause, FromGrouping, ScalarSelect, SelectBase, Values

from sqlalchemy_easy_softdelete.handler.rewriter.policy import SoftDeletePolicy
from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals
from sqlalchemy_easy_softdelete.handler.rewriter.sa.clauses import (
    aliased_table,
    join_leaves,
    joins_of,
    plain,
    relationship_target,
    subquery_in,
    ungrouped,
)


def collect_from(
    policy: SoftDeletePolicy,
    from_obj: Any,
    columns: list[Column[Any]],
    subqueries: list[FromClause],
    aliases: list[Alias],
) -> None:
    """Collect the soft-delete columns, subqueries and aliases of tables of one element of a statement's
    FROM list."""
    if isinstance(from_obj, Table):
        policy.collect_table(from_obj, columns)
        return

    if isinstance(from_obj, Join):
        # Join (and the ORM's _ORMJoin) holds two things: 'left' and 'right'. Check both.
        _collect_join(policy, from_obj, columns, subqueries, aliases)
        return

    if isinstance(from_obj, FromGrouping):
        # A parenthesized FROM element, such as a JOIN nested on the right of another JOIN.
        collect_from(policy, from_obj.element, columns, subqueries, aliases)
        return

    if subquery_in(from_obj) is not None:
        # A subquery, or an alias of one (an alias of an aliased() entity).
        subqueries.append(from_obj)
        return

    if isinstance(from_obj, TableClause) or isinstance(from_obj, TextClause):
        # TableClause/TextClause objects are raw text SQL identifiers and as such, we cannot
        # introspect or do anything about this statement.
        return

    if isinstance(from_obj, Values):
        # A VALUES (...) construct is a literal row set with no underlying table,
        # so there is nothing to apply a soft-delete filter to. Leave it untouched
        # (same as raw-text identifiers above).
        return

    if aliased_table(from_obj) is not None:
        # An alias of a table (aliased(Model)): filtered on its own soft-delete column.
        aliases.append(from_obj)
        return

    if isinstance(from_obj, Alias):
        raise NotImplementedError(f'Unsupported object "{(type(from_obj.element))}" inside Alias in statement.froms')

    raise NotImplementedError(f'Unsupported object "{(type(from_obj))}" in statement.froms')


def _collect_join(
    policy: SoftDeletePolicy,
    join: Join,
    columns: list[Column[Any]],
    subqueries: list[FromClause],
    aliases: list[Alias],
) -> None:
    """Handle multiple, and potentially recursive joins: nested joins, then subqueries, then tables and
    aliases of tables."""
    sides = (ungrouped(join.left), ungrouped(join.right))
    for side in sides:
        if isinstance(side, Join):
            _collect_join(policy, side, columns, subqueries, aliases)
    for side in sides:
        # A subquery, or an alias of one (an alias of an aliased() entity).
        if not isinstance(side, Join) and subquery_in(side) is not None:
            subqueries.append(side)
    for side in sides:
        if isinstance(side, Table):
            policy.collect_table(side, columns)
        elif aliased_table(side) is not None:
            aliases.append(side)


class FromScan:
    """The subqueries (and aliases of subqueries) a statement puts in its FROM clause, found by reading
    the statement, without compiling it.

    These are the subqueries of ``select_from()``, of joins (target, left side, the target of a
    relationship), and those the statement's columns and WHERE criteria select from, as such or
    through an ``aliased()`` entity or a mapped class. A plan is only reused when, for the statement
    it was made from, this finds the subqueries that the analysis of its FROM clause found.
    ``explicit`` holds those the statement names as a FROM: in ``select_from()``, as the target of a
    join, or as the left side of a join that does not go along a relationship.
    """

    def __init__(self, stmt: Select[Any]) -> None:
        self.subqueries: dict[int, FromClause] = {}
        self.explicit: set[int] = set()
        self.table_aliases: dict[int, Alias] = {}
        for target, onclause, from_, _flags in joins_of(stmt):
            along_relationship = isinstance(target, QueryableAttribute) or isinstance(onclause, QueryableAttribute)
            self._visit(relationship_target(target) if isinstance(target, QueryableAttribute) else target, True)
            self._visit(from_, not along_relationship)
            for attribute in (target, onclause):
                if isinstance(attribute, QueryableAttribute):
                    # A join along a relationship goes from the attribute's own entity.
                    self._visit(attribute.parent.selectable, False)
        for from_obj in internals.from_list(stmt):
            self._visit(from_obj, True)
        columns = itertools.chain(
            internals.raw_columns(stmt),
            internals.where_criteria(stmt),
            # with_only_columns() sets the statement's earlier entities aside, along with their joins.
            *(internals.raw_columns(entities) for entities in internals.set_aside_entities(stmt)),
        )
        for from_obj in itertools.chain.from_iterable(internals.from_objects(element) for element in columns):
            self._visit(from_obj, False)

    def _visit(self, from_obj: Any, explicit: bool) -> None:
        if from_obj is None:
            return
        from_obj = plain(ungrouped(from_obj))
        if isinstance(from_obj, Join):
            self._visit(from_obj.left, explicit)
            self._visit(from_obj.right, explicit)
        elif subquery_in(from_obj) is not None:
            self.subqueries.setdefault(id(from_obj), from_obj)
            if explicit:
                self.explicit.add(id(from_obj))
        elif aliased_table(from_obj) is not None:
            self.table_aliases.setdefault(id(from_obj), from_obj)


def implicit_join_lefts(stmt: Select[Any], final_froms: Any) -> dict[int, Any]:
    """The left side SQLAlchemy chose in ``final_froms`` (the analyzed FROM clause of ``stmt``) for each
    join of ``stmt`` that does not give one (``join(Target)``, ``join(Target, onclause)``), by the id of
    the join: the FROM element the join attaches to, as SQLAlchemy finds it among those of its left."""
    by_right = joins_by_right(final_froms)
    lefts: dict[int, Any] = {}
    for join in joins_of(stmt):
        target, onclause, from_, _flags = join
        if from_ is not None or isinstance(target, QueryableAttribute) or isinstance(onclause, QueryableAttribute):
            continue
        joined = by_right.get(id(plain(ungrouped(target))))
        if joined is None:
            continue
        candidates = join_leaves(joined.left)
        indexes = sql_util.find_left_clause_to_join_from(candidates, joined.right, joined.onclause)
        if len(indexes) == 1:
            lefts[id(join)] = candidates[indexes[0]]
    return lefts


def joins_by_right(final_froms: Any) -> dict[int, Join]:
    """The joins of ``final_froms`` (the analyzed FROM clause of a statement) by the id of their right side."""
    found: dict[int, Join] = {}
    stack = list(final_froms)
    while stack:
        from_obj = ungrouped(stack.pop())
        if isinstance(from_obj, Join):
            found.setdefault(id(plain(ungrouped(from_obj.right))), from_obj)
            stack.extend((from_obj.left, from_obj.right))
    return found


def joins_to_without_its_columns(stmt: Select[Any], targets: set[int]) -> bool:
    """Whether the first join of ``stmt``, with no ``select_from()``, goes to one of ``targets`` (by id) without a
    left side of its own, with an ON clause that names no table but the target (``ON true``, ``ON target.x > 0``):
    SQLAlchemy then chooses the left side among the FROMs of the statement's columns, which still include the
    original of the copy. A later join attaches to the joins before it, and an ON clause naming another table
    tells SQLAlchemy the side."""
    if internals.from_list(stmt):
        return False
    all_joins = list(
        itertools.chain(
            *(internals.joins(entities) for entities in internals.set_aside_entities(stmt)), internals.joins(stmt)
        )
    )
    if not all_joins:
        return False
    target, onclause, from_, _flags = all_joins[0]
    if (
        from_ is not None
        or onclause is None
        or isinstance(target, QueryableAttribute)
        or isinstance(onclause, QueryableAttribute)
    ):
        return False
    plain_target = plain(ungrouped(target))
    if id(plain_target) not in targets:
        return False
    return all(plain(table) is plain_target for table in _tables_named_in(onclause))


def _tables_named_in(clause: Any) -> list[Any]:
    """The tables (and other FROM elements) whose columns ``clause`` names, outside the SELECTs nested in it
    (an EXISTS names the tables of its own FROM clause, which SQLAlchemy does not join to)."""
    tables: list[Any] = []
    stack = [clause]
    while stack:
        element = stack.pop()
        if isinstance(element, (SelectBase, ScalarSelect, Exists)):
            continue
        table = getattr(element, "table", None)
        if table is not None:
            tables.append(table)
        stack.extend(element.get_children())
    return tables
