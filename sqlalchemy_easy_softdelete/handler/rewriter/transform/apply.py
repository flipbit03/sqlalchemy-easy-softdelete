"""Applying a ``SelectPlan`` to a statement: the transformations, in the order they apply."""

from __future__ import annotations

from typing import Any

from sqlalchemy.sql import Select
from sqlalchemy.sql.selectable import FromClause

from sqlalchemy_easy_softdelete.handler.rewriter.analysis.froms import FromScan
from sqlalchemy_easy_softdelete.handler.rewriter.analysis.secondary import joined_secondary_options
from sqlalchemy_easy_softdelete.handler.rewriter.context import RewriteContext, Rewriter
from sqlalchemy_easy_softdelete.handler.rewriter.plan import FromAnalysis, SelectPlan
from sqlalchemy_easy_softdelete.handler.rewriter.transform.copies import filtered_copy, place_copies
from sqlalchemy_easy_softdelete.handler.rewriter.transform.criteria import with_criteria
from sqlalchemy_easy_softdelete.handler.rewriter.transform.joins import (
    with_outer_join_filters,
    with_secondary_criteria,
)
from sqlalchemy_easy_softdelete.handler.rewriter.transform.nested import rewrite_nested_selects


def apply_plan(
    rewriter: Rewriter,
    stmt: Select[Any],
    plan: SelectPlan,
    analysis: FromAnalysis | None,
    key: Any,
    context: RewriteContext,
) -> Any:
    """Return a copy of ``stmt`` with ``plan`` applied.

    ``analysis`` holds what the analysis of the FROM clause found when it was just analyzed;
    otherwise the subqueries are found again by reading the statement.

    In order: filtered copies of the FROM clause's subqueries, the nested SELECTs rewritten, the loader options of
    joined eager loads through soft-deletable secondaries, the loader criteria, the conditions of the secondaries
    of relationship joins, then the soft-delete columns: in the ON clause of a LEFT OUTER JOIN they are on the right
    of, else in the WHERE clause.
    """
    policy = rewriter.policy
    if analysis is None and (plan.rewrites_subqueries or plan.filters_table_aliases):
        analysis = _read_analysis(rewriter, stmt, context)
    copies: dict[int, FromClause] = {}
    memo: dict[int, FromClause | None] = context.copy_memo if context.copy_memo is not None else {}
    if plan.rewrites_subqueries and analysis is not None:
        for from_obj in analysis.subqueries:
            copy = filtered_copy(rewriter, from_obj, memo, context)
            if copy is not None:
                copies[id(from_obj)] = copy
        if copies:
            stmt = place_copies(stmt, copies, analysis.join_lefts)

    if plan.rewrites_nested:
        stmt = rewrite_nested_selects(rewriter, stmt, memo, key, plan.many_froms, context)

    secondary_options = plan.secondary_options
    if secondary_options is None:
        secondary_options = tuple(joined_secondary_options(policy, stmt)[0])
    if secondary_options:
        stmt = stmt.options(*secondary_options)

    stmt = with_criteria(policy, stmt, dict(plan.criteria_entities), plan.holds_selects)

    if plan.secondary_criteria:
        stmt = with_secondary_criteria(stmt, plan.secondary_criteria)

    filters: list[Any] = list(plan.filter_columns)
    if plan.filters_table_aliases and analysis is not None and not context.relationship_load:
        for alias in analysis.table_aliases:
            column = policy.alias_column(alias)
            if column is not None:
                filters.append(column)

    if plan.outer_joins:
        stmt, filters = with_outer_join_filters(stmt, plan.outer_joins, filters, copies)

    for column in filters:
        stmt = stmt.filter(column.is_(None))
    return stmt


def _read_analysis(rewriter: Rewriter, stmt: Select[Any], context: RewriteContext) -> FromAnalysis:
    """What the analysis of the FROM clause found, read again from a statement whose plan was remembered."""
    scan = FromScan(stmt)
    aliases = [alias for alias in scan.table_aliases.values() if rewriter.policy.alias_column(alias) is not None]
    if context.relationship_load:
        aliases = []
    return FromAnalysis(list(scan.subqueries.values()), {}, aliases)
