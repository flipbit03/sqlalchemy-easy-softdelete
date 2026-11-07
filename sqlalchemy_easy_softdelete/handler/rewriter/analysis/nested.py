"""The SELECTs nested in a statement (EXISTS, IN, scalar subqueries, ``any()``, ``has()``), and what a nested
SELECT correlates to."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Mapper
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql import CompoundSelect, Select, visitors
from sqlalchemy.sql.selectable import FromClause

from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals
from sqlalchemy_easy_softdelete.handler.rewriter.sa.clauses import (
    join_leaves,
    join_target_selectable,
    joins_of,
    plain,
    selects_from_mapper,
    ungrouped,
)


def nested_selects(stmt: Select[Any]) -> Any:
    """The SELECTs nested in the WHERE and HAVING clauses of ``stmt`` and in the ON clauses of its joins (see
    ``SelectPlan.rewrites_nested``), but not those of HAVING that are also in its columns, ORDER BY or GROUP BY
    clauses (see ``listed_selects``)."""
    clauses: list[Any] = [*internals.where_criteria(stmt)]
    if internals.having_criteria(stmt):
        listed = listed_selects(stmt)
        for clause in internals.having_criteria(stmt):
            yield from (select for select in internals.top_selects(clause) if not is_listed(select, listed))
    clauses += [
        onclause
        for _target, onclause, _from, _flags in internals.joins(stmt)
        if onclause is not None and not isinstance(onclause, QueryableAttribute)
    ]
    for clause in clauses:
        yield from internals.top_selects(clause)


def listed_selects(stmt: Select[Any]) -> list[Any]:
    """The SELECTs nested in the columns, ORDER BY and GROUP BY clauses of ``stmt``, which loader criteria filter
    (see ``SelectPlan.rewrites_nested``)."""
    # A column that is a FROM element is an entity; its SELECT is its own FROM clause.
    clauses = [
        *(column for column in internals.raw_columns(stmt) if not isinstance(column, FromClause)),
        *internals.order_by(stmt),
        *internals.group_by(stmt),
    ]
    return [select for clause in clauses for select in internals.top_selects(clause)]


def is_listed(element: Any, listed: list[Any]) -> bool:
    """Whether ``element``, a SELECT of a HAVING clause, is the same expression as one of ``listed``: PostgreSQL
    matches HAVING to GROUP BY by expression, so they must render the same."""
    if not isinstance(element, (Select, CompoundSelect)):
        return False
    return any(internals.same_expression(element, select) for select in listed)


def holds_selects(stmt: Select[Any]) -> bool:
    """Whether ``stmt`` holds SELECTs anywhere (see ``SelectPlan.holds_selects``)."""
    return any(
        element is not stmt and isinstance(element, (Select, CompoundSelect)) for element in visitors.iterate(stmt)
    )


def mappers_in(element: Any) -> list[Mapper[Any]]:
    """The mappers whose tables ``element`` selects from, through their entities or columns."""
    found: dict[Mapper[Any], None] = {}
    for child in visitors.iterate(element):
        mapper = internals.annotations_of(child).get("parentmapper")
        if isinstance(mapper, Mapper) and selects_from_mapper(child, mapper):
            found.setdefault(mapper, None)
    return list(found)


def from_ids(stmt: Select[Any]) -> frozenset[int]:
    """The ids of the FROM elements of ``stmt`` that a SELECT nested in it may correlate to: those of its FROM list,
    its joins and its columns and criteria, and the tables and subqueries of the joins among them."""
    found: set[int] = set()

    def add(from_obj: Any) -> None:
        if from_obj is None:
            return
        for leaf in join_leaves(ungrouped(from_obj)):
            # A filtered copy stands for what it is a clone of, or restates (a CTE): the nested SELECT may still
            # refer to that.
            stack = [plain(leaf)]
            while stack:
                element = stack.pop()
                if element is None or id(element) in found:
                    continue
                found.add(id(element))
                stack.append(internals.is_clone_of(element))
                stack.append(internals.restates(element))

    for from_obj in internals.from_list(stmt):
        add(from_obj)
    for target, _onclause, from_, _flags in joins_of(stmt):
        add(join_target_selectable(target) if not isinstance(target, FromClause) else target)
        add(from_)
    for element in (*internals.raw_columns(stmt), *internals.where_criteria(stmt)):
        for from_obj in internals.from_objects(element):
            add(from_obj)
    return frozenset(found)


def froms_without_criteria(stmt: Select[Any]) -> list[Any]:
    """The FROM elements of ``stmt``: those SQLAlchemy finds for it without its WHERE and HAVING criteria, and
    those of the criteria that are not among them."""
    froms = list(internals.final_froms_without_loader_options(internals.without_criteria(stmt)))
    seen = {id(plain(leaf)) for from_obj in froms for leaf in join_leaves(ungrouped(from_obj))}
    for criterion in (*internals.where_criteria(stmt), *internals.having_criteria(stmt)):
        for from_obj in internals.from_objects(criterion):
            if id(plain(from_obj)) not in seen:
                seen.add(id(plain(from_obj)))
                froms.append(plain(from_obj))
    return froms


def correlated_froms(stmt: Select[Any], enclosing: frozenset[int], many_froms: bool) -> set[int]:
    """The ids of the FROM elements of the enclosing statement that ``stmt``, a nested SELECT, correlates to, as
    SQLAlchemy does: those of ``correlate()``, all but those of ``correlate_except()``, or else all of them, when
    ``stmt`` has more than one FROM element (``many_froms``; one that has only one selects from it again)."""
    correlate, correlate_except, auto_correlate = internals.correlation(stmt)
    if correlate:
        return {id(plain(from_obj)) for from_obj in correlate} & enclosing
    if correlate_except is not None:
        return set(enclosing) - {id(plain(from_obj)) for from_obj in correlate_except}
    return set(enclosing) if auto_correlate and many_froms else set()
