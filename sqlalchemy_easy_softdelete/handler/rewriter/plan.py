"""What the rewrite of one SELECT adds to it (``SelectPlan``), and the cache of plans by statement shape."""

from __future__ import annotations

import enum
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlalchemy import Column
from sqlalchemy.sql import Alias
from sqlalchemy.sql.selectable import FromClause

DEFAULT_REWRITE_CACHE_SIZE = 500
"""Default number of statement shapes whose rewrite is remembered (SQLAlchemy's default ``query_cache_size``)."""

Criteria = dict[type, bool]
"""Mapped classes to filter through ``with_loader_criteria``, each with whether the filter also applies to
its ``aliased()`` forms."""


class RewriteCacheInfo(NamedTuple):
    """Statistics of a rewriter's cache, from :meth:`SoftDeleteQueryRewriter.rewrite_cache_info`."""

    hits: int
    """SELECTs rewritten with a remembered rewrite."""

    misses: int
    """SELECTs whose FROM clause was analyzed, and whose rewrite was then remembered."""

    uncacheable: int
    """SELECTs whose FROM clause was analyzed, and whose rewrite cannot be remembered: statements
    without a cache key, and statements whose rewrite depends on what their cache key leaves out."""

    size: int
    """Statement shapes currently remembered."""

    max_size: int
    """The ``rewrite_cache_size`` the rewriter was created with."""


class Reason(enum.Enum):
    """Why a plan does not hold for every statement with the same cache key, which leaves out execution options
    and the statement's own objects (its aliases and subqueries): the plan is then made for each statement."""

    SUBQUERY_IN_LOADER_PATH = "a loader option's path goes through an aliased() entity over a subquery"
    """Whether eager loading into it is filtered depends on its statement's execution options."""

    LOAD_PATH_DEPENDENT = "a relationship load's loader options are read along its own load path"
    """Which ``aliased()`` entities of the options count depends on the load path, which cache keys leave out."""

    ALIASED_SUBQUERY_ENTITY = "an aliased() entity over a subquery that the eager loading criteria may not reach"
    """Whether the criteria may filter it depends on its statement's execution options."""

    CTE_OF_ENCLOSING = "a nested SELECT refers to a CTE of the statement it is nested in"
    """The enclosing statement's copy restates it; which CTE that is, is the statement's own object."""

    RECURSIVE_CTE_SELF_REFERENCE = "a recursive CTE's statement refers to the CTE itself"
    """It is left as it is, and which CTE that is, is the statement's own object."""

    UNREAD_SUBQUERIES = "reading the statement does not find the subqueries the analysis of its FROM clause finds"
    """A plan is only reused when the subqueries it copies are found again by reading the statement."""

    LATERAL_CORRELATION = "a LATERAL subquery's statement correlates to another LATERAL subquery"
    """Those are left as they are in it."""

    IMPLICIT_JOIN_LEFTS = "a copy changes which left side SQLAlchemy picks for a join that gives none"
    """The left sides SQLAlchemy chose are the statement's own objects."""

    UNREAD_TABLE_ALIASES = "reading the statement finds aliases of tables its FROM clause does not hold"
    """Reading the statement then cannot stand in for the analysis of its FROM clause."""

    ON_CLAUSE_REFERS_TO_STATEMENT = "an outer join's computed ON clause refers to the statement's own aliases"
    """A later statement of the same shape has its own."""


@dataclass(frozen=True)
class SelectPlan:
    """What the soft-delete rewrite of one SELECT adds to it."""

    filter_columns: tuple[Column[Any], ...]
    """Soft-delete columns of the tables in the FROM clause, filtered with ``IS NULL``."""

    criteria_entities: tuple[tuple[type, bool], ...]
    """Mapped classes filtered through ``with_loader_criteria`` options on this statement (eager loading),
    each with whether the filter also applies to the class's ``aliased()`` forms."""

    rewrites_subqueries: bool
    """Whether the FROM clause holds subqueries, which filtered copies replace in it."""

    secondary_criteria: tuple[tuple[int, Any], ...]
    """The soft-delete condition of the secondary of each join of the statement along a relationship whose
    secondary is a soft-deletable table, or a subquery of soft-deletable tables (see
    ``SoftDeletePolicy.soft_delete_condition``), by the position of the join: added to the join's ON clause with
    ``relationship.and_()``, since SQLAlchemy joins an alias of the secondary that it makes while compiling the
    statement."""

    rewrites_nested: bool
    """Whether the statement has SELECTs nested in its WHERE or HAVING clauses, or in the ON clauses of its joins
    (EXISTS, IN, scalar subqueries, ``any()``, ``has()``), which are rewritten like statements of their own: they
    correlate to the statement's own FROM elements. Those of its columns, ORDER BY and GROUP BY clauses, and those of
    HAVING that are also there, are filtered by loader criteria instead (see ``analysis.eager``), so that the
    same expression renders the same in all of them, and the statement's columns stay those SQLAlchemy keys result
    rows by."""

    many_froms: bool
    """Whether the statement has more than one FROM element, with which SQLAlchemy auto-correlates it, nested in
    another, to that one's FROM elements (see ``analysis.nested.correlated_froms``)."""

    holds_selects: bool
    """Whether the statement holds SELECTs anywhere (nested ones, subqueries, CTEs), which the loader criteria
    look through for those that disable soft-delete filtering (see ``transform.criteria.disabled_selects``)."""

    secondary_options: tuple[Any, ...] | None
    """The loader options for the joined eager loads of the statement along relationships whose secondary is
    soft-deletable (see ``analysis.secondary.joined_secondary_options``), or None when they are made for each
    statement: when their paths go through the statement's own ``aliased()`` entities."""

    outer_joins: tuple[tuple[Any, Any], ...]
    """The LEFT OUTER JOINs of the statement whose right side holds soft-deletable tables or aliases of them,
    each with the ON clause SQLAlchemy finds for it when it gives none (else None). Those tables are filtered
    in the join's ON clause, not the WHERE clause, so that a row whose joined rows are all soft-deleted is
    kept, with NULLs, as if they did not exist. A join is numbered as ``joins_of()`` lists it, or, for one
    written with Core in ``select_from()``, by ``("from", index, path)`` (see ``analysis.outer_joins``).
    A FULL OUTER JOIN keeps the rows of both sides: its tables stay filtered in the WHERE clause."""

    filters_table_aliases: bool
    """Whether the FROM clause holds aliases of soft-deletable tables that the statement names
    (``aliased(Model)``, ``with_polymorphic(..., flat=True)``), each filtered on its own soft-delete column.
    The aliases are objects of the statement, so they are found again in each statement the plan is
    applied to."""

    reasons: tuple[Reason, ...]
    """Why the plan does not hold for every statement with the same cache key; none when it does."""

    @property
    def reusable(self) -> bool:
        """Whether the plan holds for every statement with the same cache key (see ``Reason``)."""
        return not self.reasons


class FromAnalysis(NamedTuple):
    """What the analysis of one statement's FROM clause found, for the plan made from it."""

    subqueries: list[FromClause]
    """Subqueries (and aliases of subqueries) of the FROM clause that the statement names, which filtered
    copies replace."""

    join_lefts: dict[int, Any]
    """The left side SQLAlchemy chose for each join of the statement that does not give one, by the id
    of the join, when a copy joins the statement's FROM list (see ``transform.copies.place_copies``)."""

    table_aliases: list[Alias]
    """Aliases of soft-deletable tables in the FROM clause that the statement names."""


class PlanCache:
    """Plans of reusable rewrites by statement cache key, least recently used first out."""

    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        self.hits = 0
        self.misses = 0
        self.uncacheable = 0
        self._plans: OrderedDict[Any, SelectPlan] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> SelectPlan | None:
        with self._lock:
            plan = self._plans.get(key)
            if plan is not None:
                self._plans.move_to_end(key)
                self.hits += 1
            return plan

    def put(self, key: Any, plan: SelectPlan) -> None:
        with self._lock:
            self.misses += 1
            self._plans[key] = plan
            self._plans.move_to_end(key)
            while len(self._plans) > self.max_size:
                self._plans.popitem(last=False)

    def count_uncacheable(self) -> None:
        with self._lock:
            self.uncacheable += 1

    def info(self) -> RewriteCacheInfo:
        return RewriteCacheInfo(
            hits=self.hits, misses=self.misses, uncacheable=self.uncacheable, size=len(self), max_size=self.max_size
        )

    def __len__(self) -> int:
        return len(self._plans)
