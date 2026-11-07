"""Rewriting the SELECTs nested in a statement's WHERE and HAVING clauses and in the ON clauses of its joins, like
statements of their own."""

from __future__ import annotations

import itertools
from typing import Any

from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql import CompoundSelect, Select

from sqlalchemy_easy_softdelete.handler.rewriter.analysis.nested import (
    correlated_froms,
    from_ids,
    is_listed,
    listed_selects,
)
from sqlalchemy_easy_softdelete.handler.rewriter.context import RewriteContext, Rewriter
from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals


def rewrite_nested_selects(
    rewriter: Rewriter,
    stmt: Select[Any],
    memo: dict[int, Any],
    key: Any,
    many_froms: bool,
    context: RewriteContext,
) -> Any:
    """``stmt`` with the SELECTs nested in its WHERE and HAVING clauses and in the ON clauses of its joins
    rewritten like statements of their own (see ``SelectPlan.rewrites_nested``). One that disables soft-delete
    filtering is left as it is. Each one's plan is remembered by ``key`` (``stmt``'s) and its place.
    ``many_froms`` is whether ``stmt`` has more than one FROM element (see ``SelectPlan.many_froms``).

    A SELECT of HAVING that is also in the columns or GROUP BY is left, with what is nested in it, to the loader
    criteria: PostgreSQL matches HAVING to GROUP BY by expression, and SQLAlchemy 2.1.0 also applies the criteria to
    the correlated classes of a nested SELECT, so only the criteria render both the same.
    """
    froms = from_ids(stmt)
    if context.enclosing_froms:
        # Nested itself: the FROM elements it correlates to are not its own, for the SELECTs nested in it.
        froms = froms - correlated_froms(stmt, context.enclosing_froms, many_froms)
    nested = context.nested_in(froms, memo)
    place = itertools.count()

    def replace(element: Any, **kw: Any) -> Any:
        if not isinstance(element, (Select, CompoundSelect)):
            return None
        keyed = nested.keyed((key, "nested", next(place)) if key is not None else None)
        if isinstance(element, CompoundSelect):
            return rewriter.rewrite_compound_select(element, keyed)
        return rewriter.rewrite_select(element, keyed)

    parts: dict[str, Any] = {
        "where_criteria": tuple(internals.replace_selects(clause, replace) for clause in internals.where_criteria(stmt))
    }
    having = internals.having_criteria(stmt)
    if having:
        listed = listed_selects(stmt)

        def replace_unlisted(element: Any, **kw: Any) -> Any:
            # One that is also in the columns or GROUP BY is left, with what is in it, to the criteria.
            return element if is_listed(element, listed) else replace(element)

        parts["having_criteria"] = tuple(internals.replace_selects(clause, replace_unlisted) for clause in having)
    new_joins = []
    for target, onclause, from_, flags in internals.joins(stmt):
        if (
            onclause is not None
            and not isinstance(onclause, QueryableAttribute)
            and any(True for _ in internals.top_selects(onclause))
        ):
            onclause = internals.replace_selects(onclause, replace)
        new_joins.append((target, onclause, from_, flags))
    parts["joins"] = tuple(new_joins)
    return internals.copy_statement(stmt, **parts)
