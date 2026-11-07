"""What the rewrite of one SELECT adds to it (``SelectPlan``), the cache of plans by statement shape, and
``RewriteContext``: where the statement being rewritten sits."""

from __future__ import annotations

import contextvars
import enum
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from sqlalchemy import Column
from sqlalchemy.sql import Alias
from sqlalchemy.sql.selectable import FromClause

if TYPE_CHECKING:
    from sqlalchemy_easy_softdelete.handler.rewriter.policy import SoftDeletePolicy


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
    LOAD_PATH_DEPENDENT = "a relationship load's loader options are read along its own load path"
    ALIASED_SUBQUERY_ENTITY = "an aliased() entity over a subquery that the eager loading criteria may not reach"
    CTE_OF_ENCLOSING = "a nested SELECT refers to a CTE of the statement it is nested in"
    RECURSIVE_CTE_SELF_REFERENCE = "a recursive CTE's statement refers to the CTE itself"
    UNREAD_SUBQUERIES = "reading the statement does not find the subqueries the analysis of its FROM clause finds"
    LATERAL_CORRELATION = "a LATERAL subquery's statement correlates to another LATERAL subquery"
    IMPLICIT_JOIN_LEFTS = "a copy changes which left side SQLAlchemy picks for a join that gives none"
    UNREAD_TABLE_ALIASES = "reading the statement finds aliases of tables its FROM clause does not hold"
    ON_CLAUSE_REFERS_TO_STATEMENT = "an outer join's computed ON clause refers to the statement's own aliases"


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
    HAVING that are also there, are filtered by loader criteria instead (see ``eager_loaded_entities``), so that the
    same expression renders the same in all of them, and the statement's columns stay those SQLAlchemy keys result
    rows by."""

    many_froms: bool
    """Whether the statement has more than one FROM element, with which SQLAlchemy auto-correlates it, nested in
    another, to that one's FROM elements (see ``correlated_froms``)."""

    holds_selects: bool
    """Whether the statement holds SELECTs anywhere (nested ones, subqueries, CTEs), which the loader criteria
    look through for those that disable soft-delete filtering (see ``disabled_selects``)."""

    secondary_options: tuple[Any, ...] | None
    """The loader options for the joined eager loads of the statement along relationships whose secondary is
    soft-deletable (see ``joined_secondary_options``), or None when they are made for each
    statement: when their paths go through the statement's own ``aliased()`` entities."""

    outer_joins: tuple[tuple[Any, Any], ...]
    """The LEFT OUTER JOINs of the statement whose right side holds soft-deletable tables or aliases of them,
    each with the ON clause SQLAlchemy finds for it when it gives none (else None). Those tables are filtered
    in the join's ON clause, not the WHERE clause, so that a row whose joined rows are all soft-deleted is
    kept, with NULLs, as if they did not exist. A join is numbered as ``joins_of()`` lists it, or, for one
    written with Core in ``select_from()``, by ``("from", index, path)`` (see ``outer_joins``).
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
    of the join, when a copy joins the statement's FROM list (see ``place_copies``)."""

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


RELATIONSHIP_LOAD: contextvars.ContextVar[bool] = contextvars.ContextVar("relationship_load", default=False)
"""Read by a ``rewrite_statement()`` call given no context (see ``RewriteContext.relationship_load``); the hook passes
a context."""

LOAD_PATH: contextvars.ContextVar[Any] = contextvars.ContextVar("load_path", default=None)
"""Read by a ``rewrite_statement()`` call given no context (see ``RewriteContext.load_path``); the hook passes a
context."""

_NO_FROMS: frozenset[int] = frozenset()


class RewriteContext(NamedTuple):
    """Where the statement being rewritten sits; each level of a rewrite derives its own. Most rewrites share
    ``ROOT``."""

    relationship_load: bool = False
    """Whether the statement being rewritten loads a relationship (from ``ORMExecuteState.is_relationship_load``).
    Such a statement selects its parent rows by primary key, from an alias of the parent that SQLAlchemy makes:
    aliases of tables are not filtered there, so related rows of the parents a statement loaded on purpose with
    ``include_deleted`` still load."""

    load_path: Any = None
    """The loader path of the relationship load being rewritten (``ORMExecuteState.loader_strategy_path``).
    SQLAlchemy passes the parent statement's loader options on to each relationship load, those of other paths
    too: only the options on the load's own path concern its statement."""

    enclosing_froms: frozenset[int] = _NO_FROMS
    """The ids of the FROM elements of the statement the SELECT being rewritten is nested in (a SELECT in a WHERE
    clause): the nested SELECT correlates to them, so they are filtered there, not in it. Empty for a statement
    of its own (an execution's statement, or that of a subquery or CTE in a FROM clause)."""

    nested_key: Any = None
    """The key the plan of the nested SELECT being rewritten is remembered by: that of the statement it is nested
    in, and its place there, or None when that statement's plan is not remembered."""

    copy_memo: dict[int, Any] | None = None
    """The filtered copies made for the statement the SELECT being rewritten is nested in, by the id of the
    subquery or CTE each replaces: the nested SELECT uses the same ones, so that a CTE renders once."""

    in_lateral: bool = False
    """Whether the statement being rewritten is (inside) the statement of a LATERAL subquery: its references to the
    FROMs of the enclosing statement (another LATERAL subquery's columns) are correlations, not FROMs of its own."""

    ctes_in_rewrite: frozenset[int] = _NO_FROMS
    """The CTEs (by id) whose statement is being rewritten, with those they restate: inside a recursive CTE's
    statement, the references to the CTE itself are left as they are."""

    @classmethod
    def root(cls, relationship_load: bool = False, load_path: Any = None) -> RewriteContext:
        """The context of an execution's statement: ``ROOT`` unless it is a relationship load."""
        if not relationship_load and load_path is None:
            return ROOT
        return cls(relationship_load, load_path)

    @classmethod
    def ambient(cls) -> RewriteContext:
        """The root context of a ``rewrite_statement()`` call given none: from ``RELATIONSHIP_LOAD`` and ``LOAD_PATH``."""
        return cls.root(RELATIONSHIP_LOAD.get(), LOAD_PATH.get())

    def nested_in(self, froms: frozenset[int], memo: dict[int, Any]) -> RewriteContext:
        """For the SELECTs nested in a statement whose FROM elements are ``froms`` and whose copies are ``memo``."""
        return RewriteContext(
            self.relationship_load, self.load_path, froms, self.nested_key, memo, self.in_lateral, self.ctes_in_rewrite
        )

    def keyed(self, nested_key: Any) -> RewriteContext:
        """For a nested SELECT (or union member) whose plan is remembered by ``nested_key``."""
        return RewriteContext(
            self.relationship_load,
            self.load_path,
            self.enclosing_froms,
            nested_key,
            self.copy_memo,
            self.in_lateral,
            self.ctes_in_rewrite,
        )

    def unnested(self) -> RewriteContext:
        """For the statement of a subquery or CTE of a FROM clause: it correlates to nothing, and makes copies of
        its own."""
        return RewriteContext(
            self.relationship_load,
            self.load_path,
            _NO_FROMS,
            self.nested_key,
            None,
            self.in_lateral,
            self.ctes_in_rewrite,
        )

    def lateral(self) -> RewriteContext:
        """For what is inside a LATERAL subquery."""
        return RewriteContext(
            self.relationship_load,
            self.load_path,
            self.enclosing_froms,
            self.nested_key,
            self.copy_memo,
            True,
            self.ctes_in_rewrite,
        )

    def in_ctes(self, ids: set[int]) -> RewriteContext:
        """For the statement of a CTE: ``ids`` are the CTE and those it restates."""
        return RewriteContext(
            self.relationship_load,
            self.load_path,
            self.enclosing_froms,
            self.nested_key,
            self.copy_memo,
            self.in_lateral,
            self.ctes_in_rewrite | ids,
        )


ROOT = RewriteContext()
"""The context of a statement that is not a relationship load, rewritten as one of its own."""


class Rewriter(Protocol):
    """What the planner and the transformations need from the rewriter: its policy, and rewriting the statements
    nested in the one being rewritten."""

    policy: SoftDeletePolicy

    def rewrite_select(self, stmt: Any, context: RewriteContext | None = None) -> Any: ...

    def rewrite_compound_select(self, stmt: Any, context: RewriteContext | None = None) -> Any: ...
