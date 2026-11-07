"""Planning a SELECT's rewrite: a remembered plan for its shape, or a new one from what the analysis finds."""

from __future__ import annotations

from typing import Any

import sqlalchemy
from sqlalchemy import Column
from sqlalchemy.sql import Alias, Select
from sqlalchemy.sql.selectable import FromClause, Lateral

from sqlalchemy_easy_softdelete.handler.rewriter.analysis.eager import (
    eager_loaded_entities,
    keep_criteria_off_unfilterable_aliases,
)
from sqlalchemy_easy_softdelete.handler.rewriter.analysis.froms import (
    FromScan,
    collect_from,
    implicit_join_lefts,
    joins_to_without_its_columns,
)
from sqlalchemy_easy_softdelete.handler.rewriter.analysis.nested import (
    correlated_froms,
    froms_without_criteria,
    holds_selects,
    nested_selects,
)
from sqlalchemy_easy_softdelete.handler.rewriter.analysis.outer_joins import outer_joins, refers_to_statement_objects
from sqlalchemy_easy_softdelete.handler.rewriter.analysis.secondary import (
    joined_secondary_options,
    reusable_options,
    secondary_criteria,
)
from sqlalchemy_easy_softdelete.handler.rewriter.context import RewriteContext, Rewriter
from sqlalchemy_easy_softdelete.handler.rewriter.plan import Criteria, FromAnalysis, PlanCache, Reason, SelectPlan
from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals
from sqlalchemy_easy_softdelete.handler.rewriter.sa.clauses import cte_root, plain, subquery_in, ungrouped
from sqlalchemy_easy_softdelete.handler.rewriter.transform.copies import filtered_copy


def plan_for(
    rewriter: Rewriter, stmt: Select[Any], context: RewriteContext, cache: PlanCache | None
) -> tuple[SelectPlan, FromAnalysis | None, Any]:
    """The plan for ``stmt``, a remembered one for its shape or a new one, and, for a new one, what
    the analysis of its FROM clause found; and the key it is remembered by, if any.
    """
    # Inside a CTE's own statement, the plan depends on which CTE is being rewritten, which cache keys
    # leave out (its references to itself are left as they are).
    if context.ctes_in_rewrite:
        cache = None
    key: Any = None
    if cache is not None:
        if context.enclosing_froms:
            # A nested SELECT's plan depends on what it correlates to: it is remembered by the key of the
            # statement it is nested in and its place there (see transform.nested).
            key = context.nested_key
        else:
            cache_key = internals.cache_key(stmt)
            # A relationship load's plan leaves out aliases of tables (see RewriteContext.relationship_load).
            key = (context.relationship_load, cache_key.key) if cache_key is not None else None
    if cache is not None and key is not None:
        plan = cache.get(key)
        if plan is not None:
            return plan, None, key

    plan, analysis = plan_select(rewriter, stmt, context)
    if cache is not None:
        if key is not None and plan.reusable:
            cache.put(key, plan)
        else:
            cache.count_uncacheable()
    return plan, analysis, key if plan.reusable else None


def plan_select(rewriter: Rewriter, stmt: Select[Any], context: RewriteContext) -> tuple[SelectPlan, FromAnalysis]:
    """Work out what the soft-delete rewrite adds to ``stmt``, and the subqueries of its FROM clause
    (or aliases of subqueries), which filtered copies replace.
    """
    policy = rewriter.policy
    reasons: list[Reason] = []
    final_froms, many_froms = _own_froms(stmt, context)
    columns: list[Column[Any]] = []
    from_subqueries: list[FromClause] = []
    from_aliases: list[Alias] = []
    for from_obj in final_froms:
        collect_from(policy, from_obj, columns, from_subqueries, from_aliases)

    entities = _eager_criteria(rewriter, stmt, context, reasons)
    found = _subqueries_to_copy(from_subqueries, context, reasons)
    scan = FromScan(stmt) if found or from_aliases else None
    subqueries: list[FromClause] = []
    join_lefts: dict[int, Any] = {}
    if found and scan is not None:
        subqueries, join_lefts = _placed_subqueries(rewriter, stmt, found, scan, final_froms, context, reasons)
    table_aliases: list[Alias] = []
    if from_aliases and scan is not None and not context.relationship_load:
        table_aliases = _filtered_table_aliases(rewriter, from_aliases, scan, reasons)

    joins = outer_joins(stmt, final_froms, columns, table_aliases)
    if any(refers_to_statement_objects(onclause) for _, onclause in joins):
        # The ON clause SQLAlchemy found for a join that gives none refers to this statement's aliases or
        # subqueries: a later statement of the same shape has its own.
        reasons.append(Reason.ON_CLAUSE_REFERS_TO_STATEMENT)
    plan = SelectPlan(
        tuple(columns),
        tuple(entities.items()),
        rewrites_subqueries=bool(subqueries),
        secondary_criteria=secondary_criteria(policy, stmt),
        rewrites_nested=any(True for _ in nested_selects(stmt)),
        many_froms=many_froms,
        holds_selects=holds_selects(stmt),
        secondary_options=reusable_options(*joined_secondary_options(policy, stmt)),
        outer_joins=joins,
        filters_table_aliases=bool(table_aliases),
        reasons=tuple(dict.fromkeys(reasons)),
    )
    return plan, FromAnalysis(subqueries, join_lefts, table_aliases)


def _own_froms(stmt: Select[Any], context: RewriteContext) -> tuple[list[Any], bool]:
    """The FROM elements SQLAlchemy finds for ``stmt``, but those it correlates to when nested, and whether it has
    more than one (see ``SelectPlan.many_froms``)."""
    enclosing = context.enclosing_froms
    try:
        final_froms = list(internals.final_froms_without_loader_options(stmt))
    except sqlalchemy.exc.InvalidRequestError:
        # A nested SELECT, on its own: one nested in its criteria may correlate to all of its FROM elements,
        # which SQLAlchemy 2.1 refuses there. Its FROM elements are those without its criteria, and theirs.
        if not enclosing:
            raise
        final_froms = froms_without_criteria(stmt)
    many_froms = len(final_froms) > 1
    if enclosing:
        correlated_ids = correlated_froms(stmt, enclosing, many_froms)
        final_froms = [from_obj for from_obj in final_froms if id(plain(ungrouped(from_obj))) not in correlated_ids]
    return final_froms, many_froms


def _eager_criteria(rewriter: Rewriter, stmt: Select[Any], context: RewriteContext, reasons: list[Reason]) -> Criteria:
    """The classes eager loading, the mapping and the statement's listed SELECTs load, which loader criteria
    filter (see ``analysis.eager``)."""
    eager_entities, eager_reasons, mapped_aliases = eager_loaded_entities(rewriter.policy, stmt)
    reasons.extend(eager_reasons)
    # Eager loading joins aliases of the loaded classes: their filter applies to aliased() forms.
    entities: Criteria = dict.fromkeys(eager_entities, True)
    if entities:
        # It depends on execution options inside the statement, which cache keys leave out.
        reasons.extend(keep_criteria_off_unfilterable_aliases(rewriter.policy, stmt, entities, context, mapped_aliases))
    return entities


def _subqueries_to_copy(
    from_subqueries: list[FromClause], context: RewriteContext, reasons: list[Reason]
) -> dict[int, FromClause]:
    """The subqueries of the FROM clause that get a filtered copy, by id: not a CTE of the statement the SELECT is
    nested in, nor a recursive CTE's reference to itself."""
    found: dict[int, FromClause] = {}
    for from_obj in from_subqueries:
        plain_from = plain(from_obj)
        if context.enclosing_froms and internals.cte_lineage(subquery_in(plain_from)) & context.enclosing_froms:
            # A CTE of an enclosing statement (or an alias of one), in a nested SELECT: the enclosing statement's
            # filtered copy restates it, and SQLAlchemy renders that copy once for every reference.
            reasons.append(Reason.CTE_OF_ENCLOSING)
            continue
        if context.ctes_in_rewrite and id(cte_root(subquery_in(plain_from))) in context.ctes_in_rewrite:
            # A recursive CTE's reference to itself, inside its own statement: left as it is.
            reasons.append(Reason.RECURSIVE_CTE_SELF_REFERENCE)
            continue
        found.setdefault(id(plain_from), plain_from)
    return found


def _placed_subqueries(
    rewriter: Rewriter,
    stmt: Select[Any],
    found: dict[int, FromClause],
    scan: FromScan,
    final_froms: list[Any],
    context: RewriteContext,
    reasons: list[Reason],
) -> tuple[list[FromClause], dict[int, Any]]:
    """The subqueries the copies replace, as the statement names them, and the left sides of its joins that a copy
    joining its FROM list would change (see ``FromAnalysis``)."""
    if scan.subqueries.keys() != found.keys():
        # A later statement of this shape would not find its subqueries without this analysis.
        reasons.append(Reason.UNREAD_SUBQUERIES)
    # Only the subqueries the statement names can be replaced; SQLAlchemy builds the others
    # (an alias of a relationship's secondary) while it compiles the statement.
    subqueries = [subquery for key, subquery in found.items() if key in scan.subqueries]
    correlated = [s for s in subqueries if isinstance(s, Lateral) and id(s) not in scan.explicit]
    if correlated and context.in_lateral:
        # Inside a LATERAL subquery's statement, another LATERAL subquery's columns refer to the enclosing
        # statement's FROM clause, which filters it.
        subqueries = [s for s in subqueries if not any(s is c for c in correlated)]
        reasons.append(Reason.LATERAL_CORRELATION)
    elif correlated:
        raise NotImplementedError(
            "A LATERAL subquery the statement selects from without joining it is not supported: "
            "join it, as in .join(lateral_subquery, true())"
        )
    join_lefts: dict[int, Any] = {}
    if any(id(subquery) not in scan.explicit for subquery in subqueries) or joins_to_without_its_columns(
        stmt, {id(subquery) for subquery in subqueries if _may_be_copied(rewriter, subquery, context)}
    ):
        # Copies of subqueries the statement does not name as a FROM join its FROM list. A first join
        # to a copy ON true (as a LATERAL subquery is joined), or on the copy's own columns, does not
        # tell SQLAlchemy its left side, among the FROMs the columns of the original bring.
        join_lefts = implicit_join_lefts(stmt, final_froms)
        if join_lefts:
            reasons.append(Reason.IMPLICIT_JOIN_LEFTS)
    return subqueries, join_lefts


def _filtered_table_aliases(
    rewriter: Rewriter, from_aliases: list[Alias], scan: FromScan, reasons: list[Reason]
) -> list[Alias]:
    """The aliases of soft-deletable tables in the FROM clause that the statement names, each filtered on its own
    soft-delete column."""
    found_aliases = {id(plain(alias)) for alias in from_aliases}
    if any(key not in found_aliases for key in scan.table_aliases):
        # The scan finds an alias the FROM clause does not hold: it cannot stand in for the analysis.
        reasons.append(Reason.UNREAD_TABLE_ALIASES)
    # Only the aliases the statement names can be filtered; SQLAlchemy builds the others (an alias of
    # a relationship's secondary table) while it compiles the statement.
    return [
        alias
        for key, alias in scan.table_aliases.items()
        if key in found_aliases and rewriter.policy.alias_column(alias) is not None
    ]


def _may_be_copied(rewriter: Rewriter, subquery: FromClause, context: RewriteContext) -> bool:
    """Whether a subquery of the statement gets a filtered copy, here or in a statement of the same shape:
    it does when its statement is filtered, and may when part of it includes soft-deleted rows, which
    cache keys leave out."""
    inner = subquery_in(subquery)
    if inner is None:
        return False
    return (
        filtered_copy(rewriter, subquery, {}, context) is not None
        or not rewriter.policy.filtered_mappers_in(inner.element)[1]
    )
