"""Main query rewriter logic."""

from __future__ import annotations

import contextvars
import itertools
import re
import threading
import types
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple, cast

import sqlalchemy
from sqlalchemy import Column, Table, and_, exists, inspect, or_
from sqlalchemy.orm import FromStatement, Mapper, RelationshipProperty
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.orm.strategy_options import Load
from sqlalchemy.orm.util import AliasedClass, AliasedInsp, LoaderCriteriaOption
from sqlalchemy.sql import Alias, CompoundSelect, Executable, Join, Select, Subquery, TableClause, visitors
from sqlalchemy.sql import util as sql_util
from sqlalchemy.sql.annotation import Annotated
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.elements import ColumnClause, TextClause
from sqlalchemy.sql.lambdas import LambdaElement
from sqlalchemy.sql.selectable import (
    CTE,
    AliasedReturnsRows,
    Exists,
    FromClause,
    FromGrouping,
    Lateral,
    ScalarSelect,
    SelectBase,
    SelectStatementGrouping,
    TextualSelect,
    Values,
)
from sqlalchemy.sql.visitors import InternalTraversal

from sqlalchemy_easy_softdelete.hook import IgnoredTable

# Type alias for statements we can rewrite
Statement = Select | FromStatement | CompoundSelect | Executable | LambdaElement

_SA_VERSION = tuple(int(part) for part in re.findall(r"\d+", sqlalchemy.__version__)[:3])
SECONDARY_CRITERIA_SUPPORTED = _SA_VERSION >= (2, 0, 28)
"""Whether SQLAlchemy adapts the criteria of ``relationship.and_()`` that refer to the relationship's
secondary to the alias of it it joins (from 2.0.28 on): soft-deletable secondary tables, and subqueries of
them, are only filtered in relationship joins from that release on."""

RELATIONSHIP_LOAD: contextvars.ContextVar[bool] = contextvars.ContextVar("relationship_load", default=False)
"""Whether the statement being rewritten loads a relationship (set by the hook from
``ORMExecuteState.is_relationship_load``). Such a statement selects its parent rows by primary key, from an
alias of the parent that SQLAlchemy makes: aliases of tables are not filtered there, so related rows of the
parents a statement loaded on purpose with ``include_deleted`` still load."""

_IN_LATERAL: contextvars.ContextVar[bool] = contextvars.ContextVar("in_lateral", default=False)
"""Whether the statement being rewritten is the statement of a LATERAL subquery: its references to the FROMs
of the enclosing statement (another LATERAL subquery's columns) are correlations, not FROMs of its own."""

_CTES_IN_REWRITE: contextvars.ContextVar[frozenset[int]] = contextvars.ContextVar(
    "ctes_in_rewrite", default=frozenset()
)
"""The CTEs (by id) whose statement is being rewritten, with those they restate: inside a recursive CTE's
statement, the references to the CTE itself are left as they are."""

LOAD_PATH: contextvars.ContextVar[Any] = contextvars.ContextVar("load_path", default=None)
"""The loader path of the relationship load being rewritten (``ORMExecuteState.loader_strategy_path``, set by the
hook). SQLAlchemy passes the parent statement's loader options on to each relationship load, those of other
paths too: only the options on the load's own path concern its statement."""

DEFAULT_REWRITE_CACHE_SIZE = 500
"""Default number of statement shapes whose rewrite is remembered (SQLAlchemy's default ``query_cache_size``)."""


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


_Criteria = dict[type, bool]
"""Mapped classes to filter through ``with_loader_criteria``, each with whether the filter also applies to
its ``aliased()`` forms."""


@dataclass(frozen=True)
class _SelectPlan:
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
    secondary is a soft-deletable table, or a subquery of soft-deletable tables (see ``_soft_delete_condition``),
    by the position of the join: added to the join's ON clause with ``relationship.and_()``, since SQLAlchemy
    joins an alias of the secondary that it makes while compiling the statement."""

    outer_joins: tuple[tuple[Any, Any], ...]
    """The LEFT OUTER JOINs of the statement whose right side holds soft-deletable tables or aliases of them,
    each with the ON clause SQLAlchemy finds for it when it gives none (else None). Those tables are filtered
    in the join's ON clause, not the WHERE clause, so that a row whose joined rows are all soft-deleted is
    kept, with NULLs, as if they did not exist. A join is numbered as ``_joins_of()`` lists it, or, for one
    written with Core in ``select_from()``, by ``("from", index, path)`` (see ``_outer_joins_in``).
    A FULL OUTER JOIN keeps the rows of both sides: its tables stay filtered in the WHERE clause."""

    filters_table_aliases: bool
    """Whether the FROM clause holds aliases of soft-deletable tables that the statement names
    (``aliased(Model)``, ``with_polymorphic(..., flat=True)``), each filtered on its own soft-delete column.
    The aliases are objects of the statement, so they are found again in each statement the plan is
    applied to."""

    reusable: bool
    """Whether the plan holds for every statement with the same cache key.

    It does not when it depends on execution options of a statement inside a subquery, which cache
    keys leave out, nor when the subqueries a statement holds cannot be found again without analyzing
    its FROM clause.
    """


class _FromAnalysis(NamedTuple):
    """What the analysis of one statement's FROM clause found, for the plan made from it."""

    subqueries: list[FromClause]
    """Subqueries (and aliases of subqueries) of the FROM clause that the statement names, which filtered
    copies replace."""

    join_lefts: dict[int, Any]
    """The left side SQLAlchemy chose for each join of the statement that does not give one, by the id
    of the join, when a copy joins the statement's FROM list (see ``_place_copies``)."""

    table_aliases: list[Alias]
    """Aliases of soft-deletable tables in the FROM clause that the statement names."""


class _PlanCache:
    """Plans of reusable rewrites by statement cache key, least recently used first out."""

    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        self.hits = 0
        self.misses = 0
        self.uncacheable = 0
        self._plans: OrderedDict[Any, _SelectPlan] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> _SelectPlan | None:
        with self._lock:
            plan = self._plans.get(key)
            if plan is not None:
                self._plans.move_to_end(key)
                self.hits += 1
            return plan

    def put(self, key: Any, plan: _SelectPlan) -> None:
        with self._lock:
            self.misses += 1
            self._plans[key] = plan
            self._plans.move_to_end(key)
            while len(self._plans) > self.max_size:
                self._plans.popitem(last=False)

    def count_uncacheable(self) -> None:
        with self._lock:
            self.uncacheable += 1

    def __len__(self) -> int:
        return len(self._plans)


class SoftDeleteQueryRewriter:
    """Rewrites SQL statements based on configuration.

    Rewriting never modifies the statement it is given, nor any object inside it: it returns a new
    statement. Applications can therefore reuse statements, subqueries and ``lambda_stmt`` objects
    across executions, with and without soft-delete filtering. The new statement differs only in its
    FROM clause, where filtered copies of subqueries replace them, and keeps the caller's columns,
    entities and options, which SQLAlchemy keys result rows by (``row._mapping[subquery.c.x]``).

    Finding the tables of an ORM SELECT means running SQLAlchemy's ORM compilation of it, outside of
    SQLAlchemy's compiled cache. The rewriter remembers what it adds to a statement by the statement's
    cache key, which SQLAlchemy gives two statements only when they compile to the same SQL, so later
    statements of the same shape skip that step.
    """

    def __init__(
        self,
        deleted_field_name: str,
        disable_soft_delete_option_name: str,
        ignored_tables: list[IgnoredTable] | None = None,
        rewrite_cache_size: int = DEFAULT_REWRITE_CACHE_SIZE,
    ):
        """
        Instantiate a new query rewriter.

        Params:

        deleted_field_name:
            The name of the field that should be present in a table for soft-deletion
            rewriting to occur

        disable_soft_delete_option_name:
            Execution option name (to use with .execution_options(xxxx=True) to disable
            soft deletion rewriting in a query

        ignored_tables:
            Tables that are never filtered, even when they have the soft-delete field

        rewrite_cache_size:
            How many statement shapes to remember the rewrite of; 0 disables the cache

        """
        if rewrite_cache_size < 0:
            raise ValueError(f"rewrite_cache_size must be 0 or more, got {rewrite_cache_size}")

        """List of table names that should be ignored from soft-deletion"""
        self.ignored_tables = ignored_tables or []
        self.deleted_field_name = deleted_field_name
        self.disable_soft_delete_option_name = disable_soft_delete_option_name
        self._is_not_deleted = _is_null_criteria(deleted_field_name)
        self._lambda_cache_token = _LambdaRewriteToken(deleted_field_name)
        self._plan_cache = _PlanCache(rewrite_cache_size) if rewrite_cache_size else None
        self._mapped_subquery_conditions: dict[type, Any] = {}

    def rewrite_cache_info(self) -> RewriteCacheInfo:
        """Hits, misses and size of the rewrite cache, to help choose ``rewrite_cache_size``."""
        cache = self._plan_cache
        if cache is None:
            return RewriteCacheInfo(hits=0, misses=0, uncacheable=0, size=0, max_size=0)
        return RewriteCacheInfo(
            hits=cache.hits,
            misses=cache.misses,
            uncacheable=cache.uncacheable,
            size=len(cache),
            max_size=cache.max_size,
        )

    def rewrite_statement(self, stmt: Statement) -> Statement:
        """Rewrite a single SQL-like Statement."""
        if isinstance(stmt, Select):
            return self.rewrite_select(stmt)

        # Handle CompoundSelect
        if isinstance(stmt, CompoundSelect):
            return self.rewrite_compound_select(stmt)

        # Handle LambdaElement (StatementLambdaElement, LinkedLambdaElement)
        # Used by lambda_stmt() for statement caching (e.g., advanced-alchemy)
        if isinstance(stmt, LambdaElement):
            return self.rewrite_lambda_element(stmt)

        # Handle FromStatement which is also a Select/Executable
        if isinstance(stmt, FromStatement):
            if self._is_disabled(stmt):
                return stmt
            # The element is what SQLAlchemy compiles: loader criteria apply from there.
            element: Any
            if isinstance(stmt.element, Select):
                element = self.rewrite_select(stmt.element)
            elif isinstance(stmt.element, CompoundSelect):
                element = self.rewrite_compound_select(stmt.element)
            else:
                # INSERT/UPDATE/DELETE ... RETURNING, and textual SQL, are left as they are.
                return stmt
            if element is stmt.element:
                return stmt
            rewritten = stmt._generate()
            rewritten.element = element
            return rewritten

        raise NotImplementedError(f'Unsupported statement type "{(type(stmt))}"!')

    def rewrite_lambda_element(self, stmt: LambdaElement) -> LambdaElement:
        """Rewrite a LambdaElement (from lambda_stmt()).

        LambdaElement wraps a SQL statement for caching purposes. The actual
        statement is accessed via the _resolved attribute. The rewritten statement
        is set on a copy of the LambdaElement; the one passed in is left as it was.

        Supports both StatementLambdaElement (basic lambda_stmt) and
        LinkedLambdaElement (chained lambda operations).
        """
        # Get the resolved statement - this is the actual Select/CompoundSelect
        resolved = stmt._resolved

        # Rewrite based on the type of the resolved statement
        rewritten_resolved: Any
        if isinstance(resolved, Select):
            rewritten_resolved = self.rewrite_select(resolved)
        elif isinstance(resolved, CompoundSelect):
            rewritten_resolved = self.rewrite_compound_select(resolved)
        elif isinstance(resolved, FromStatement):
            rewritten_resolved = self.rewrite_statement(resolved)
        else:
            raise NotImplementedError(f'Unsupported statement type "{type(resolved)}" inside LambdaElement._resolved')

        if rewritten_resolved is resolved:
            return stmt
        rewritten = stmt._clone()
        rewritten._resolved = rewritten_resolved
        # SQLAlchemy caches the compiled form of a lambda statement by the lambda's code and closure,
        # not by its resulting statement, so the copy would share the compiled form of the statement
        # without soft-delete filtering (from a Core execution of the same lambda, which this hook does
        # not rewrite). This rewriter's token in the cache key keeps the two apart. The rewrite only
        # adds IS NULL criteria, so the lambda's bound parameters stay the statement's parameters.
        if isinstance(rewritten.closure_cache_key, tuple):
            rewritten.closure_cache_key = rewritten.closure_cache_key + (self._lambda_cache_token,)
        return rewritten

    def rewrite_select(self, stmt: Select) -> Select:
        """Rewrite a Select Statement."""
        # if the user tagged this query with an execution_option to disable soft-delete filtering
        # simply return back the same stmt
        if self._is_disabled(stmt):
            return stmt

        plan, analysis = self._plan_for(stmt)
        return self._apply_plan(stmt, plan, analysis)

    def rewrite_compound_select(self, stmt: CompoundSelect) -> CompoundSelect:
        """Rewrite a Compound Select Statement."""
        if self._is_disabled(stmt):
            return stmt
        selects: list[SelectBase] = [self._rewrite_compound_member(select) for select in stmt.selects]
        if all(new is old for new, old in zip(selects, stmt.selects)):
            return stmt
        rewritten = stmt._generate()
        rewritten.selects = selects
        return rewritten

    def _rewrite_compound_member(self, member: Any) -> Any:
        """Rewrite one SELECT of a union: a SELECT or a nested union, possibly in parentheses (a member
        with ORDER BY or LIMIT, or a nested union, is held in a grouping)."""
        if isinstance(member, SelectStatementGrouping):
            element = self._rewrite_compound_member(member.element)
            return member if element is member.element else SelectStatementGrouping(element)
        if isinstance(member, CompoundSelect):
            return self.rewrite_compound_select(member)
        if not isinstance(member, Select):
            # A member of textual SQL (text(...).columns(...)): there is nothing to filter.
            return member
        return self.rewrite_select(member)

    def _is_disabled(self, stmt: Any) -> bool:
        """Whether the statement disables soft-delete filtering with the configured execution option."""
        return bool(stmt.get_execution_options().get(self.disable_soft_delete_option_name))

    def _with_criteria(self, stmt: Any, criteria: _Criteria) -> Any:
        """``stmt`` with ``with_loader_criteria`` options for ``criteria`` (``_SoftDeleteCriteria``, which leaves
        out the SELECTs that disable soft-delete filtering)."""
        if not criteria:
            return stmt
        disabled = self._disabled_from_selects(stmt)
        options = []
        for entity, include_aliases in criteria.items():
            if hasattr(entity, self.deleted_field_name):
                where, configuration = self._is_not_deleted, None
            else:
                where, configuration = self._mapped_subquery_condition(entity), self._configuration()
            options.append(
                _SoftDeleteCriteria(
                    entity,
                    where,
                    include_aliases=include_aliases,
                    disable_option_name=self.disable_soft_delete_option_name,
                    disabled=disabled,
                    configuration=configuration,
                )
            )
        return stmt.options(*options)

    def _configuration(self) -> tuple[str, str, tuple[tuple[str, str | None], ...]]:
        """What a rewriter of the same configuration is made from: see ``_SoftDeleteCriteria.__reduce__``."""
        ignored = tuple((table.name, table.table_schema) for table in self.ignored_tables)
        return (self.deleted_field_name, self.disable_soft_delete_option_name, ignored)

    def _disabled_from_selects(self, stmt: Any) -> frozenset[int]:
        """The ids of the SELECTs the eager loading criteria leave out (see ``_SoftDeleteCriteria``): those of
        the subqueries in the FROM clause of ``stmt``, or in that of the subqueries in it, at any depth, that
        disable soft-delete filtering, with what is nested in them, and the SELECTs they are clones of."""
        disabled: set[int] = set()
        self._collect_disabled_from_selects(stmt, disabled, set())
        return frozenset(disabled)

    def _collect_disabled_from_selects(self, stmt: Any, disabled: set[int], seen: set[int]) -> None:
        if isinstance(stmt, CompoundSelect):
            for member in stmt.selects:
                self._collect_disabled_from_selects(member, disabled, seen)
            return
        if not isinstance(stmt, Select):
            return
        for from_obj in _FromScan(stmt).subqueries.values():
            subquery = _subquery_in(from_obj)
            # A recursive CTE is in the FROM clause of its own statement.
            if subquery is None or id(subquery) in seen:
                continue
            seen.add(id(subquery))
            if not self._is_disabled(subquery.element):
                self._collect_disabled_from_selects(subquery.element, disabled, seen)
                continue
            for element in visitors.iterate(subquery.element):
                if isinstance(element, (Select, CompoundSelect)):
                    disabled.add(id(element))
                    if element._is_clone_of is not None:
                        disabled.add(id(element._is_clone_of))

    def _plan_for(self, stmt: Select[Any]) -> tuple[_SelectPlan, _FromAnalysis | None]:
        """The plan for ``stmt``, a remembered one for its shape or a new one, and, for a new one, what
        the analysis of its FROM clause found.
        """
        # Inside a CTE's own statement, the plan depends on which CTE is being rewritten, which cache keys
        # leave out (its references to itself are left as they are).
        cache = self._plan_cache if not _CTES_IN_REWRITE.get() else None
        cache_key = stmt._generate_cache_key() if cache is not None else None
        # A relationship load's plan leaves out aliases of tables (see RELATIONSHIP_LOAD).
        key = (RELATIONSHIP_LOAD.get(), cache_key.key) if cache_key is not None else None
        if cache is not None and key is not None:
            plan = cache.get(key)
            if plan is not None:
                return plan, None

        plan, analysis = self._plan_select(stmt)
        if cache is not None:
            if key is not None and plan.reusable:
                cache.put(key, plan)
            else:
                cache.count_uncacheable()
        return plan, analysis

    def _plan_select(self, stmt: Select[Any]) -> tuple[_SelectPlan, _FromAnalysis]:
        """Work out what the soft-delete rewrite adds to ``stmt``, and the subqueries of its FROM clause
        (or aliases of subqueries), which filtered copies replace.
        """
        columns: list[Column[Any]] = []
        from_subqueries: list[FromClause] = []
        from_aliases: list[Alias] = []
        final_froms = _final_froms_without_loader_options(stmt)
        for from_obj in final_froms:
            self._collect_from(from_obj, columns, from_subqueries, from_aliases)

        # Eager loading joins aliases of the loaded classes: their filter applies to aliased() forms.
        eager_entities, reusable = self._eager_loaded_entities(stmt)
        entities: _Criteria = dict.fromkeys(eager_entities, True)
        if entities and not self._keep_eager_criteria_off_unfilterable_aliases(stmt, entities):
            # It depends on execution options inside the statement, which cache keys leave out.
            reusable = False
        found: dict[int, FromClause] = {}
        in_rewrite = _CTES_IN_REWRITE.get()
        for from_obj in from_subqueries:
            plain = _plain(from_obj)
            if in_rewrite and id(_cte_root(_subquery_in(plain))) in in_rewrite:
                # A recursive CTE's reference to itself, inside its own statement: left as it is.
                reusable = False
                continue
            found.setdefault(id(plain), plain)

        subqueries: list[FromClause] = []
        join_lefts: dict[int, Any] = {}
        scan = _FromScan(stmt) if found or from_aliases else None
        if found and scan is not None:
            if scan.subqueries.keys() != found.keys():
                # A later statement of this shape would not find its subqueries without this analysis.
                reusable = False
            # Only the subqueries the statement names can be replaced; SQLAlchemy builds the others
            # (an alias of a relationship's secondary) while it compiles the statement.
            subqueries = [subquery for key, subquery in found.items() if key in scan.subqueries]
            correlated = [s for s in subqueries if isinstance(s, Lateral) and id(s) not in scan.explicit]
            if correlated and _IN_LATERAL.get():
                # Inside a LATERAL subquery's statement, another LATERAL subquery's columns refer to the enclosing
                # statement's FROM clause, which filters it.
                subqueries = [s for s in subqueries if not any(s is c for c in correlated)]
                reusable = False
            elif correlated:
                raise NotImplementedError(
                    "A LATERAL subquery the statement selects from without joining it is not supported: "
                    "join it, as in .join(lateral_subquery, true())"
                )
            if any(id(subquery) not in scan.explicit for subquery in subqueries) or _joins_to_without_its_columns(
                stmt, {id(subquery) for subquery in subqueries if self._may_be_copied(subquery)}
            ):
                # Copies of subqueries the statement does not name as a FROM join its FROM list. A first join
                # to a copy ON true (as a LATERAL subquery is joined), or on the copy's own columns, does not
                # tell SQLAlchemy its left side, among the FROMs the columns of the original bring.
                join_lefts = _implicit_join_lefts(stmt, final_froms)
                if join_lefts:
                    reusable = False

        table_aliases: list[Alias] = []
        if from_aliases and scan is not None and not RELATIONSHIP_LOAD.get():
            found_aliases = {id(_plain(alias)) for alias in from_aliases}
            if any(key not in found_aliases for key in scan.table_aliases):
                # The scan finds an alias the FROM clause does not hold: it cannot stand in for the analysis.
                reusable = False
            # Only the aliases the statement names can be filtered; SQLAlchemy builds the others (an alias of
            # a relationship's secondary table) while it compiles the statement.
            table_aliases = [
                alias
                for key, alias in scan.table_aliases.items()
                if key in found_aliases and self._alias_column(alias) is not None
            ]

        outer_joins = self._outer_joins(stmt, final_froms, columns, table_aliases)
        if any(_refers_to_statement_objects(onclause) for _, onclause in outer_joins):
            # The ON clause SQLAlchemy found for a join that gives none refers to this statement's aliases or
            # subqueries: a later statement of the same shape has its own.
            reusable = False
        plan = _SelectPlan(
            tuple(columns),
            tuple(entities.items()),
            rewrites_subqueries=bool(subqueries),
            secondary_criteria=self._secondary_criteria(stmt),
            outer_joins=outer_joins,
            filters_table_aliases=bool(table_aliases),
            reusable=reusable,
        )
        return plan, _FromAnalysis(subqueries, join_lefts, table_aliases)

    def _keep_eager_criteria_off_unfilterable_aliases(self, stmt: Select[Any], entities: _Criteria) -> bool:
        """Turn off, in ``entities``, the eager loading criteria's reach into the ``aliased()`` forms of
        each class that has, anywhere in ``stmt`` (its elements, the ``of_type()`` entities of its joins,
        the paths of its loader options), an ``aliased()`` entity over a subquery they must not filter: one whose statement includes soft-deleted rows, or one that does not pass on the
        soft-delete column (SQLAlchemy would then filter the class's table, which the statement does not
        select from there). Return whether the result holds for every statement of the same shape: it
        does not when such entities are over subqueries, whose execution options cache keys leave out.
        """
        reusable = True
        seen: set[int] = set()
        path_entities, path_dependent = _loader_path_entities(stmt)
        if path_dependent:
            # It depends on the path of the relationship load, which cache keys leave out.
            reusable = False
        for insp in itertools.chain(
            (_annotated_entity(element) for element in visitors.iterate(stmt)),
            _of_type_entities(stmt),
            path_entities,
        ):
            if not isinstance(insp, AliasedInsp) or id(insp) in seen or insp.mapper.class_ not in entities:
                continue
            seen.add(id(insp))
            subquery = _subquery_in(insp.selectable)
            if subquery is None:
                continue
            reusable = False
            column = insp.mapper.columns.get(self.deleted_field_name)
            passed_on = column is not None and insp.selectable.corresponding_column(column) is not None
            if not passed_on or not self._filtered_mappers_in(subquery.element)[1]:
                entities[insp.mapper.class_] = False
        return reusable

    def _apply_plan(self, stmt: Select[Any], plan: _SelectPlan, analysis: _FromAnalysis | None) -> Select[Any]:
        """Return a copy of ``stmt`` with ``plan`` applied.

        ``analysis`` holds what the analysis of the FROM clause found when it was just analyzed;
        otherwise the subqueries are found again by reading the statement.
        """
        if analysis is None and (plan.rewrites_subqueries or plan.filters_table_aliases):
            scan = _FromScan(stmt)
            aliases = [alias for alias in scan.table_aliases.values() if self._alias_column(alias) is not None]
            if RELATIONSHIP_LOAD.get():
                aliases = []
            analysis = _FromAnalysis(list(scan.subqueries.values()), {}, aliases)
        copies: dict[int, FromClause] = {}
        if plan.rewrites_subqueries and analysis is not None:
            memo: dict[int, FromClause | None] = {}
            for from_obj in analysis.subqueries:
                copy = self._filtered_copy(from_obj, memo)
                if copy is not None:
                    copies[id(from_obj)] = copy
            if copies:
                stmt = _place_copies(stmt, copies, analysis.join_lefts)

        stmt = self._with_criteria(stmt, dict(plan.criteria_entities))

        if plan.secondary_criteria:
            stmt = _with_secondary_criteria(stmt, plan.secondary_criteria)

        filters: list[Any] = list(plan.filter_columns)
        if plan.filters_table_aliases and analysis is not None and not RELATIONSHIP_LOAD.get():
            for alias in analysis.table_aliases:
                column = self._alias_column(alias)
                if column is not None:
                    filters.append(column)

        if plan.outer_joins:
            stmt, filters = _with_outer_join_filters(stmt, plan.outer_joins, filters, copies)

        for column in filters:
            stmt = stmt.filter(column.is_(None))
        return stmt

    def _may_be_copied(self, subquery: FromClause) -> bool:
        """Whether a subquery of the statement gets a filtered copy, here or in a statement of the same shape:
        it does when its statement is filtered, and may when part of it includes soft-deleted rows, which
        cache keys leave out."""
        inner = _subquery_in(subquery)
        if inner is None:
            return False
        return self._filtered_copy(subquery, {}) is not None or not self._filtered_mappers_in(inner.element)[1]

    def _filtered_copy(self, from_obj: FromClause, memo: dict[int, FromClause | None]) -> FromClause | None:
        """A copy of a subquery (or of an alias of one) around its rewritten statement, or None when the
        rewrite leaves the statement as it is. ``memo`` keeps the copies made for one statement."""
        key = id(from_obj)
        if key not in memo:
            element: Any = cast(AliasedReturnsRows, from_obj).element
            inner: Any
            if isinstance(from_obj, CTE) and from_obj._cte_alias is not None:
                # An alias of a CTE (cte.alias()): an alias of the CTE's filtered copy, which the WITH clause
                # renders.
                aliased_copy = self._filtered_copy(from_obj._cte_alias, memo)
                if aliased_copy is None:
                    memo[key] = None
                    return None
                copy = from_obj._clone()
                copy._is_clone_of = from_obj
                copy._cte_alias = cast(CTE, aliased_copy)
                copy.element = cast(CTE, aliased_copy).element
                copy._reset_column_collection()
                memo[key] = copy
                return copy
            if isinstance(from_obj, CTE):
                # The statement of a recursive CTE refers to the CTE itself (the CTE it restates).
                restated = {id(from_obj)}
                cte: Any = from_obj
                while cte._restates is not None:
                    cte = cte._restates
                    restated.add(id(cte))
                cte_token = _CTES_IN_REWRITE.set(_CTES_IN_REWRITE.get() | restated)
                try:
                    inner = self._rewrite_subquery_element(element)
                finally:
                    _CTES_IN_REWRITE.reset(cte_token)
                unchanged = inner is element
            elif isinstance(from_obj, Subquery):
                inner = self._rewrite_subquery_element(element)
                unchanged = inner is element
            else:
                # An alias of a subquery (an alias of an aliased() entity, or of a subquery's alias, or LATERAL)
                token = _IN_LATERAL.set(True) if isinstance(from_obj, Lateral) else None
                try:
                    inner = self._filtered_copy(element, memo)
                finally:
                    if token is not None:
                        _IN_LATERAL.reset(token)
                unchanged = inner is None
            filtered = None if unchanged else _copy_selectable(from_obj, inner)
            if isinstance(filtered, CTE) and filtered._restates is None:
                # A CTE renders once, by name: where the statement still refers to the original (in a statement
                # that includes soft-deleted rows, through an alias in a nested SELECT), SQLAlchemy renders the
                # copy, which restates it.
                filtered._restates = cast(CTE, from_obj)
            memo[key] = filtered
        return memo[key]

    def _collect_from(
        self, from_obj: Any, columns: list[Column[Any]], subqueries: list[FromClause], aliases: list[Alias]
    ) -> None:
        """Collect the soft-delete columns, subqueries and aliases of tables of one element of a statement's
        FROM list."""
        if isinstance(from_obj, Table):
            self._collect_table(from_obj, columns)
            return

        if isinstance(from_obj, Join):
            # Join (and the ORM's _ORMJoin) holds two things: 'left' and 'right'. Check both.
            self._collect_join(from_obj, columns, subqueries, aliases)
            return

        if isinstance(from_obj, FromGrouping):
            # A parenthesized FROM element, such as a JOIN nested on the right of another JOIN.
            self._collect_from(from_obj.element, columns, subqueries, aliases)
            return

        if _subquery_in(from_obj) is not None:
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

        if _aliased_table(from_obj) is not None:
            # An alias of a table (aliased(Model)): filtered on its own soft-delete column.
            aliases.append(from_obj)
            return

        if isinstance(from_obj, Alias):
            raise NotImplementedError(
                f'Unsupported object "{(type(from_obj.element))}" inside Alias in statement.froms'
            )

        raise NotImplementedError(f'Unsupported object "{(type(from_obj))}" in statement.froms')

    def _collect_join(
        self, join: Join, columns: list[Column[Any]], subqueries: list[FromClause], aliases: list[Alias]
    ) -> None:
        """Handle multiple, and potentially recursive joins: nested joins, then subqueries, then tables and
        aliases of tables."""
        sides = (_ungrouped(join.left), _ungrouped(join.right))
        for side in sides:
            if isinstance(side, Join):
                self._collect_join(side, columns, subqueries, aliases)
        for side in sides:
            # A subquery, or an alias of one (an alias of an aliased() entity).
            if not isinstance(side, Join) and _subquery_in(side) is not None:
                subqueries.append(side)
        for side in sides:
            if isinstance(side, Table):
                self._collect_table(side, columns)
            elif _aliased_table(side) is not None:
                aliases.append(side)

    def _secondary_criteria(self, stmt: Select[Any]) -> tuple[tuple[int, Any], ...]:
        """See ``_SelectPlan.secondary_criteria``."""
        if not SECONDARY_CRITERIA_SUPPORTED:
            return ()
        criteria: list[tuple[int, Any]] = []
        for index, (target, onclause, _from, _flags) in enumerate(_joins_of(stmt)):
            attribute = onclause if isinstance(onclause, QueryableAttribute) else target
            if not isinstance(attribute, QueryableAttribute):
                continue
            prop = attribute.property
            if not isinstance(prop, RelationshipProperty) or prop.secondary is None:
                continue
            condition = self._soft_delete_condition(prop.secondary)
            if condition is not None:
                criteria.append((index, condition))
        return tuple(criteria)

    def _outer_joins(
        self, stmt: Select[Any], final_froms: Any, columns: list[Column[Any]], table_aliases: list[Alias]
    ) -> tuple[tuple[Any, Any], ...]:
        """See ``_SelectPlan.outer_joins``."""
        filtered = {id(column.table) for column in columns} | {id(_plain(alias)) for alias in table_aliases}
        if not filtered:
            return ()

        def holds_filtered(selectable: Any) -> bool:
            return any(id(_plain(leaf)) in filtered for leaf in _join_leaves(selectable))

        found: list[tuple[Any, Any]] = []
        for index, from_obj in enumerate(stmt._from_obj):
            for path, join in _outer_joins_in(from_obj):
                if holds_filtered(join.right):
                    found.append((("from", index, path), None))
        joins_by_right: dict[int, Join] | None = None
        for position, (target, onclause, _from, flags) in enumerate(_joins_of(stmt)):
            if not flags.get("isouter") or flags.get("full"):
                continue
            selectable = _join_target_selectable(target)
            if selectable is None or not holds_filtered(selectable):
                continue
            computed = None
            if onclause is None and not isinstance(target, QueryableAttribute):
                if joins_by_right is None:
                    joins_by_right = _joins_by_right(final_froms)
                joined = joins_by_right.get(id(_plain(_ungrouped(selectable))))
                if joined is None or joined.onclause is None:
                    continue
                computed = joined.onclause
            found.append((position, computed))
        return tuple(found)

    def _soft_delete_condition(self, secondary: Any, *, outer_joined: bool = False) -> Any:
        """The condition that leaves out the soft-deleted rows of a table, or of the soft-deletable tables of a
        subquery (on the soft-delete column it passes on, or through the primary key it passes on), or None:
        for a relationship's secondary, or a class mapped to a subquery.

        A table the subquery outer joins leaves NULLs for its soft-deleted rows, which keeps the subquery's row:
        it is left out, unless ``outer_joined``. A mapped class's joined eager load selects the subquery's
        columns, where those rows' values would show: its condition leaves out such rows too.
        """
        if isinstance(secondary, Table):
            columns: list[Column[Any]] = []
            self._collect_table(secondary, columns)
            return columns[0].is_(None) if columns else None
        subquery = _subquery_in(secondary)
        if subquery is None or not isinstance(subquery.element, Select) or self._is_disabled(subquery.element):
            return None
        conditions = []
        for from_obj in subquery.element.get_final_froms():
            for table in _join_leaves(from_obj) if outer_joined else _inner_joined_leaves(from_obj):
                if not isinstance(table, Table):
                    continue
                columns = []
                self._collect_table(table, columns)
                if not columns:
                    continue
                passed_on = secondary.corresponding_column(columns[0])
                if passed_on is not None:
                    conditions.append(passed_on.is_(None))
                    continue
                keys = [(column, secondary.corresponding_column(column)) for column in table.primary_key.columns]
                if not keys or any(proxy is None for _, proxy in keys):
                    continue
                # An alias of the table, which SQLAlchemy does not adapt to its alias of the secondary. A NULL key
                # (the table is outer joined, without a row) passes, as a NULL soft-delete column would.
                rows = table.alias()
                conditions.append(
                    or_(
                        and_(*(proxy.is_(None) for _, proxy in keys)),
                        exists().where(
                            *(rows.c[column.key] == proxy for column, proxy in keys),
                            rows.c[columns[0].key].is_(None),
                        ),
                    )
                )
        return and_(*conditions) if conditions else None

    def _alias_column(self, alias: Alias) -> Any:
        """The soft-delete column of an alias of a table, or None when the table is ignored or has none."""
        table = cast(Table, _aliased_table(alias))
        columns: list[Column[Any]] = []
        self._collect_table(table, columns)
        return alias.corresponding_column(columns[0]) if columns else None

    def _collect_table(self, table: Table, columns: list[Column[Any]]) -> None:
        """Collect the soft-delete column of ``table``, unless it is ignored or has none."""
        if any(ignored.match_name(table) for ignored in self.ignored_tables):
            return

        # Caveat: The automatic "bool(column_obj)" conversion actually returns a truthy value of False (?),
        # so we have to explicitly compare against None
        column_obj = table.columns.get(self.deleted_field_name)
        if column_obj is not None:
            columns.append(column_obj)

    def _rewrite_subquery_element(self, element: Any) -> Any:
        """Rewrite the statement inside a subquery."""
        if isinstance(element, CompoundSelect):
            return self.rewrite_compound_select(element)

        if isinstance(element, Select):
            return self.rewrite_select(element)

        if isinstance(element, UpdateBase):
            # INSERT/UPDATE/DELETE ... RETURNING in a CTE: its rows are the statement's, not a table's.
            return element

        if isinstance(element, TextualSelect):
            # Raw SQL (text(...).columns(...)): there is nothing to filter, as for raw SQL in the FROM clause.
            return element

        raise NotImplementedError(f'Unsupported object "{(type(element))}" in subquery.element')

    def _filtered_mappers_in(self, element: Any) -> tuple[list[Mapper[Any]], bool]:
        """The mappers that the statement inside a subquery refers to, leaving out SELECTs that disable
        soft-delete filtering (and what is nested in them), and whether none of it disables it.
        """
        found: dict[Mapper[Any], None] = {}
        fully_filtered = True
        stack = [element]
        while stack:
            child = stack.pop()
            if isinstance(child, (Select, CompoundSelect)) and self._is_disabled(child):
                fully_filtered = False
                continue
            if isinstance(child, TextualSelect):
                # Raw SQL is left as it is: its rows may include soft-deleted ones.
                fully_filtered = False
                continue
            mapper = getattr(child, "_annotations", {}).get("parentmapper")
            if isinstance(mapper, Mapper) and _selects_from_mapper(child, mapper):
                found.setdefault(mapper, None)
            stack.extend(child.get_children())
        return list(found), fully_filtered

    def _eager_loaded_entities(self, stmt: Select[Any]) -> tuple[list[type], bool]:
        """Soft-deletable entities named in the loader options' paths (joinedload, subqueryload, etc.),
        and whether the list holds for every statement of the same shape.

        These are filtered with ``with_loader_criteria``, so the soft-delete filter also reaches the
        JOINed aliases and the separate queries that eager loading creates. Column loader options
        (``load_only``, ``defer``) name their entity too; their criteria repeat a filter the statement
        already has, which changes nothing.

        A class mapped to a SELECT that leaves out the soft-delete column is filtered through its primary key
        (see ``_mapped_subquery_condition``) where it is joined eager loaded.

        An ``aliased()`` entity over a subquery whose statement includes soft-deleted rows is left out:
        eager loading into it loads its rows as they are. That depends on the execution options of the
        statement inside the subquery, which cache keys leave out.
        """
        # Access _with_options - an internal attribute for ORM loader options.
        # This is part of SQLAlchemy's _traverse_internals (dp_executable_options),
        # meaning it's stable across versions. There is no public API to read options.
        entities: dict[type, None] = {}
        reusable = True
        for opt in stmt._with_options:
            if not isinstance(opt, Load):
                continue

            # An option holds a loader for each path it loads, including those its options() add:
            # entities (mappers, aliased() entities) and the relationships between them.
            loaders = [(loader.path, getattr(loader, "strategy", None)) for loader in opt.context]
            for path, strategy in loaders or [(opt.path, None)]:
                # PathRegistry is iterable at runtime but not typed as such
                path_elements: list[Any] = list(path) if path is not None else []  # type: ignore[call-overload]
                joined = strategy is not None and ("lazy", "joined") in strategy
                for position, path_element in enumerate(path_elements):
                    if isinstance(path_element, AliasedInsp):
                        if position == 0:
                            # An aliased() entity the option starts from is in the statement's FROM
                            # clause, which the rewrite handles; the entities it loads are the ones to filter.
                            continue
                        subquery = _subquery_in(path_element.selectable)
                        if subquery is not None:
                            reusable = False
                            if not self._filtered_mappers_in(subquery.element)[1]:
                                continue
                    entity_class = self._extract_entity_class_from_path_element(path_element)
                    if entity_class is None:
                        continue
                    if self._is_soft_deletable_entity(entity_class) or (
                        # Only a joined eager load into it, which the path of its loader ends at: elsewhere it is in
                        # a FROM clause, as a filtered copy of its SELECT.
                        joined
                        and position == len(path_elements) - 1
                        and self._mapped_subquery_condition(entity_class) is not None
                    ):
                        entities.setdefault(entity_class, None)

        return list(entities), reusable

    def _is_soft_deletable_entity(self, entity_class: type) -> bool:
        """Whether ``entity_class`` has the soft-delete column and its table is not ignored."""
        if not hasattr(entity_class, self.deleted_field_name):
            return False

        table = getattr(entity_class, "__table__", None)
        if isinstance(table, Table) and any(ignored.match_name(table) for ignored in self.ignored_tables):
            return False

        return True

    def _mapped_subquery_condition(self, entity_class: type) -> Any:
        """For a class mapped to a SELECT of soft-deletable tables that leaves out their soft-delete column, the
        condition that leaves out its soft-deleted rows (see ``_soft_delete_condition``), or None.

        It is the class's eager loading criteria: SQLAlchemy joins an alias of the SELECT that it makes while
        it compiles a joined eager load, which the rewrite does not replace, and adapts the criteria to it.
        """
        if entity_class not in self._mapped_subquery_conditions:
            condition = None
            mapper: Any = inspect(entity_class, raiseerr=False)
            if (
                isinstance(mapper, Mapper)
                and not hasattr(entity_class, self.deleted_field_name)
                and _subquery_in(mapper.local_table) is not None
            ):
                condition = _on_attributes(self._soft_delete_condition(mapper.local_table, outer_joined=True), mapper)
            return self._mapped_subquery_conditions.setdefault(entity_class, condition)
        return self._mapped_subquery_conditions[entity_class]

    def _extract_entity_class_from_path_element(self, path_element: Any) -> type | None:
        """The entity class of a loader option's path element: a mapper, or an ``aliased()`` entity."""
        if isinstance(path_element, Mapper):
            return path_element.class_

        # An aliased() entity in the path (`joinedload(Parent.children.of_type(alias))`)
        if isinstance(path_element, AliasedInsp):
            return path_element.mapper.class_

        return None


def _final_froms_without_loader_options(stmt: Select[Any]) -> Any:
    """The FROM list of ``stmt`` as written, before eager loading changes it.

    Eager loading adds joins to aliases of the loaded tables, which ``with_loader_criteria`` filters,
    and around a LIMIT it moves the whole statement into a subquery, which takes the statement's
    filters along. Leaving the loader options out also spares the analysis the eager loaders' setup.

    The analysis runs on a copy: SQLAlchemy's ORM compilation stores its compile options on the
    statement it compiles, which changes the statement's cache key.
    """
    bare = stmt._generate()
    bare._with_options = ()
    return bare.get_final_froms()


class _FromScan:
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
        for target, onclause, from_, _flags in _joins_of(stmt):
            along_relationship = isinstance(target, QueryableAttribute) or isinstance(onclause, QueryableAttribute)
            self._visit(_relationship_target(target) if isinstance(target, QueryableAttribute) else target, True)
            self._visit(from_, not along_relationship)
            for attribute in (target, onclause):
                if isinstance(attribute, QueryableAttribute):
                    # A join along a relationship goes from the attribute's own entity.
                    self._visit(attribute.parent.selectable, False)
        for from_obj in stmt._from_obj:
            self._visit(from_obj, True)
        columns = itertools.chain(
            stmt._raw_columns,
            stmt._where_criteria,
            # with_only_columns() sets the statement's earlier entities aside, along with their joins.
            *(entities._raw_columns for entities in stmt._memoized_select_entities),
        )
        for from_obj in itertools.chain.from_iterable(element._from_objects for element in columns):
            self._visit(from_obj, False)

    def _visit(self, from_obj: Any, explicit: bool) -> None:
        if from_obj is None:
            return
        from_obj = _plain(_ungrouped(from_obj))
        if isinstance(from_obj, Join):
            self._visit(from_obj.left, explicit)
            self._visit(from_obj.right, explicit)
        elif _subquery_in(from_obj) is not None:
            self.subqueries.setdefault(id(from_obj), from_obj)
            if explicit:
                self.explicit.add(id(from_obj))
        elif _aliased_table(from_obj) is not None:
            self.table_aliases.setdefault(id(from_obj), from_obj)


def _aliased_table(from_obj: Any) -> Table | None:
    """The table ``from_obj`` is an alias of (through any number of aliases), if it is one."""
    if not isinstance(from_obj, Alias):
        return None
    while isinstance(from_obj, Alias):
        from_obj = from_obj.element
    return from_obj if isinstance(from_obj, Table) else None


def _on_attributes(condition: Any, mapper: Mapper[Any]) -> Any:
    """``condition`` on the columns of the selectable ``mapper`` maps, with each of them replaced by its mapped
    attribute: SQLAlchemy adapts loader criteria to an alias of the class through those. None when one of the
    columns is not mapped."""
    if condition is None:
        return None
    columns = {id(column): column for column in mapper.local_table.columns}
    unmapped = False

    def replace(element: Any, **kw: Any) -> Any:
        nonlocal unmapped
        column = columns.get(id(element))
        if column is None:
            return None
        try:
            prop = mapper.get_property_by_column(column)
        except sqlalchemy.orm.exc.UnmappedColumnError:
            unmapped = True
            return None
        return getattr(mapper.class_, prop.key).expression

    replaced = visitors.replacement_traverse(condition, {}, replace)
    return None if unmapped else replaced


def _with_secondary_criteria(stmt: Select[Any], criteria: tuple[tuple[int, Any], ...]) -> Select[Any]:
    """``stmt`` with the soft-delete condition of each secondary table in ``criteria`` added to its join along
    the relationship (``relationship.and_()``). Joins are numbered as ``_joins_of()`` lists them: the
    statement's own, then those ``with_only_columns()`` set aside."""
    conditions = dict(criteria)

    def add(joins: tuple[Any, ...], offset: int) -> tuple[Any, ...]:
        rewritten = []
        for position, (target, onclause, from_, flags) in enumerate(joins, start=offset):
            condition = conditions.get(position)
            if condition is not None:
                if isinstance(onclause, QueryableAttribute):
                    onclause = onclause.and_(condition)
                else:
                    target = cast(QueryableAttribute[Any], target).and_(condition)
            rewritten.append((target, onclause, from_, flags))
        return tuple(rewritten)

    rewritten = stmt._generate()
    rewritten._setup_joins = add(stmt._setup_joins, 0)
    offset = len(stmt._setup_joins)
    if stmt._memoized_select_entities:
        memoized = []
        for entities in stmt._memoized_select_entities:
            joins = add(entities._setup_joins, offset)
            offset += len(entities._setup_joins)
            if any(position in conditions for position in range(offset - len(joins), offset)):
                entities = entities._clone()
                entities._setup_joins = joins
            memoized.append(entities)
        rewritten._memoized_select_entities = tuple(memoized)
    return rewritten


def _of_type_entities(stmt: Select[Any]) -> list[Any]:
    """The ``of_type()`` entities of the relationship joins of ``stmt``, which iterating the statement does not
    reach: ``join(Parent.children.of_type(alias))``."""
    found: list[Any] = []
    for target, onclause, _from, _flags in _joins_of(stmt):
        for attribute in (target, onclause):
            if isinstance(attribute, QueryableAttribute) and attribute._of_type is not None:
                found.append(inspect(attribute._of_type))
    return found


def _loader_path_entities(stmt: Select[Any]) -> tuple[list[Any], bool]:
    """The ``aliased()`` entities in the paths of the loader options of ``stmt``
    (``selectinload(Parent.children.of_type(alias))``), which iterating the statement does not reach, and
    whether the result depends on the path of the relationship load being rewritten (see ``LOAD_PATH``): in a
    relationship load, only the paths on the load's own path count."""
    found: list[Any] = []
    own = list(LOAD_PATH.get() or ()) if RELATIONSHIP_LOAD.get() else None
    path_dependent = False
    for opt in stmt._with_options:
        if not isinstance(opt, Load):
            continue
        for path in [loader.path for loader in opt.context] or [opt.path]:
            path_elements: list[Any] = list(path) if path is not None else []  # type: ignore[call-overload]
            aliases = [element for element in path_elements if isinstance(element, AliasedInsp)]
            if not aliases:
                continue
            if own is not None:
                path_dependent = True
                shared = min(len(own), len(path_elements))
                if any(a is not b for a, b in zip(own[:shared], path_elements[:shared])):
                    continue
            found.extend(aliases)
    return found, path_dependent


def _joins_of(stmt: Select[Any]) -> Any:
    """The joins of ``stmt``, including those ``with_only_columns()`` set aside."""
    return itertools.chain(stmt._setup_joins, *(entities._setup_joins for entities in stmt._memoized_select_entities))


def _implicit_join_lefts(stmt: Select[Any], final_froms: Any) -> dict[int, Any]:
    """The left side SQLAlchemy chose in ``final_froms`` (the analyzed FROM clause of ``stmt``) for each
    join of ``stmt`` that does not give one (``join(Target)``, ``join(Target, onclause)``), by the id of
    the join: the FROM element the join attaches to, as SQLAlchemy finds it among those of its left."""
    joins_by_right = _joins_by_right(final_froms)
    lefts: dict[int, Any] = {}
    for join in _joins_of(stmt):
        target, onclause, from_, _flags = join
        if from_ is not None or isinstance(target, QueryableAttribute) or isinstance(onclause, QueryableAttribute):
            continue
        joined = joins_by_right.get(id(_plain(_ungrouped(target))))
        if joined is None:
            continue
        candidates = _join_leaves(joined.left)
        indexes = sql_util.find_left_clause_to_join_from(candidates, joined.right, joined.onclause)
        if len(indexes) == 1:
            lefts[id(join)] = candidates[indexes[0]]
    return lefts


def _joins_by_right(final_froms: Any) -> dict[int, Join]:
    """The joins of ``final_froms`` (the analyzed FROM clause of a statement) by the id of their right side."""
    joins_by_right: dict[int, Join] = {}
    stack = list(final_froms)
    while stack:
        from_obj = _ungrouped(stack.pop())
        if isinstance(from_obj, Join):
            joins_by_right.setdefault(id(_plain(_ungrouped(from_obj.right))), from_obj)
            stack.extend((from_obj.left, from_obj.right))
    return joins_by_right


def _join_target_selectable(target: Any) -> Any:
    """The selectable a join of a statement joins to: that of a relationship's target (or its ``of_type()``
    entity), of an entity, or a table."""
    if isinstance(target, QueryableAttribute):
        entity = _relationship_entity(target)
        return entity.selectable if entity is not None else None
    insp = inspect(target, raiseerr=False)
    return getattr(insp, "selectable", None)


def _outer_joins_in(from_obj: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Join]]:
    """The LEFT OUTER JOINs of a join tree written with Core (or ``orm.join()``), each with its path from the
    root (``l``/``r`` steps)."""
    from_obj = _ungrouped(from_obj)
    if not isinstance(from_obj, Join):
        return []
    found = _outer_joins_in(from_obj.left, path + ("l",)) + _outer_joins_in(from_obj.right, path + ("r",))
    if from_obj.isouter and not from_obj.full:
        found.append((path, from_obj))
    return found


def _join_at(from_obj: Any, path: tuple[str, ...]) -> Join:
    for step in path:
        from_obj = _ungrouped(from_obj)
        from_obj = from_obj.left if step == "l" else from_obj.right
    return cast(Join, _ungrouped(from_obj))


def _refers_to_statement_objects(onclause: Any) -> bool:
    """Whether ``onclause`` refers to a FROM other than a table (an alias, a subquery): one of its statement's."""
    if onclause is None:
        return False
    return any(not isinstance(from_obj, Table) for from_obj in onclause._from_objects)


def _with_outer_join_filters(
    stmt: Select[Any], outer_joins: tuple[tuple[Any, Any], ...], filters: list[Any], copies: dict[int, FromClause]
) -> tuple[Select[Any], list[Any]]:
    """``stmt`` with the soft-delete columns of ``filters`` that belong to the right side of one of its
    ``outer_joins`` filtered in that join's ON clause, and the columns left for the WHERE clause.

    The right sides are read from ``stmt`` itself, whose aliases of tables are its own. A join that gives no
    ON clause gets the one SQLAlchemy found, on the copies of the subqueries it refers to (``copies``).
    """
    remaining = {id(column): column for column in filters}

    def take(selectable: Any) -> list[Any]:
        leaves = {id(_plain(leaf)) for leaf in _join_leaves(selectable)}
        moved = [column for column in remaining.values() if id(_plain(column.table)) in leaves]
        for column in moved:
            del remaining[id(column)]
        return [column.is_(None) for column in moved]

    from_objs = list(stmt._from_obj)
    by_position: dict[int, tuple[list[Any], Any]] = {}
    for position, computed in outer_joins:
        if isinstance(position, tuple):
            _kind, index, path = position
            join = _join_at(from_objs[index], path)
            conditions = take(join.right)
            if conditions:
                from_objs[index] = _with_on_conditions(from_objs[index], join, conditions)
        else:
            by_position[position] = ([], _on_copies(computed, copies) if computed is not None else None)
    joins = list(_joins_of(stmt))
    for position, (conditions, _computed) in by_position.items():
        conditions.extend(take(_join_target_selectable(joins[position][0])))

    def add(setup_joins: tuple[Any, ...], offset: int) -> tuple[Any, ...]:
        rewritten = []
        for position, join in enumerate(setup_joins, start=offset):
            conditions, computed = by_position.get(position, ([], None))
            rewritten.append(_join_with_conditions(join, conditions, computed) if conditions else join)
        return tuple(rewritten)

    rewritten = stmt._generate()
    rewritten._from_obj = tuple(from_objs)
    rewritten._setup_joins = add(stmt._setup_joins, 0)
    offset = len(stmt._setup_joins)
    if stmt._memoized_select_entities:
        memoized = []
        for entities in stmt._memoized_select_entities:
            joins_set_aside = add(entities._setup_joins, offset)
            offset += len(entities._setup_joins)
            if any(new is not old for new, old in zip(joins_set_aside, entities._setup_joins)):
                entities = entities._clone()
                entities._setup_joins = joins_set_aside
            memoized.append(entities)
        rewritten._memoized_select_entities = tuple(memoized)
    return rewritten, list(remaining.values())


def _join_with_conditions(join: tuple[Any, ...], conditions: list[Any], computed: Any) -> tuple[Any, ...]:
    """A join of a statement (a ``_setup_joins`` entry) with ``conditions`` added to its ON clause: to the
    relationship it joins along, or to its ON clause (``computed``, for a join that gives none)."""
    target, onclause, from_, flags = join
    if isinstance(target, QueryableAttribute) and onclause is None:
        return (target.and_(*conditions), onclause, from_, flags)
    if isinstance(onclause, QueryableAttribute):
        return (target, onclause.and_(*conditions), from_, flags)
    return (target, and_(cast(Any, onclause if onclause is not None else computed), *conditions), from_, flags)


def _with_on_conditions(from_obj: Any, join: Join, conditions: list[Any]) -> Any:
    """``from_obj``, a join tree written with Core, with ``conditions`` added to the ON clause of ``join``."""
    replacement = join._clone()
    replacement.onclause = and_(cast(Any, join.onclause), *conditions)

    def replace(element: Any, **kw: Any) -> Any:
        return replacement if element is join else None

    return visitors.replacement_traverse(from_obj, {}, replace)


def _on_copies(clause: Any, copies: dict[int, FromClause]) -> Any:
    """``clause`` with the columns of the subqueries copied in ``copies`` replaced by those of the copies."""
    if not copies:
        return clause

    def replace(element: Any, **kw: Any) -> Any:
        table = element.table if isinstance(element, ColumnClause) else None
        copy = copies.get(id(_plain(table))) if table is not None else None
        return copy.c[element.key] if copy is not None else None

    return visitors.replacement_traverse(clause, {}, replace)


def _joins_to_without_its_columns(stmt: Select[Any], targets: set[int]) -> bool:
    """Whether the first join of ``stmt``, with no ``select_from()``, goes to one of ``targets`` (by id) without a
    left side of its own, with an ON clause that names no table but the target (``ON true``, ``ON target.x > 0``):
    SQLAlchemy then chooses the left side among the FROMs of the statement's columns, which still include the
    original of the copy. A later join attaches to the joins before it, and an ON clause naming another table
    tells SQLAlchemy the side."""
    if stmt._from_obj:
        return False
    joins = list(
        itertools.chain(*(entities._setup_joins for entities in stmt._memoized_select_entities), stmt._setup_joins)
    )
    if not joins:
        return False
    target, onclause, from_, _flags = joins[0]
    if (
        from_ is not None
        or onclause is None
        or isinstance(target, QueryableAttribute)
        or isinstance(onclause, QueryableAttribute)
    ):
        return False
    plain_target = _plain(_ungrouped(target))
    if id(plain_target) not in targets:
        return False
    return all(_plain(table) is plain_target for table in _tables_named_in(onclause))


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


def _inner_joined_leaves(from_obj: Any) -> list[Any]:
    """The leaves of a join that each of its rows has a row of: those not on the right of a LEFT OUTER JOIN."""
    from_obj = _ungrouped(from_obj)
    if not isinstance(from_obj, Join):
        return [from_obj]
    left = _inner_joined_leaves(from_obj.left)
    if from_obj.isouter and not from_obj.full:
        return left
    return left + _inner_joined_leaves(from_obj.right)


def _join_leaves(from_obj: Any) -> list[Any]:
    """The tables, subqueries and other FROM elements a join is made of, in order."""
    from_obj = _ungrouped(from_obj)
    if isinstance(from_obj, Join):
        return _join_leaves(from_obj.left) + _join_leaves(from_obj.right)
    return [from_obj]


def _place_copies(stmt: Select[Any], copies: dict[int, FromClause], join_lefts: dict[int, Any]) -> Select[Any]:
    """A copy of ``stmt`` that selects from ``copies``, keyed by the id of the subquery (or alias of a
    subquery) that each one replaces.

    Only the FROM clause changes. The statement's columns, criteria, entities and loader options keep
    referring to the original subqueries: SQLAlchemy keys result rows by those objects
    (``row._mapping[subquery.c.x]``, ``row._mapping[alias]``), and the ORM recognizes entities by
    identity. They render the same against a copy, which has the same name and columns, and
    SQLAlchemy leaves a subquery out of the FROM clause when a clone of it is there, which the copies
    are:

    - where the statement names a subquery as a FROM (``select_from()``, the target or left side of a
      join, the target of a join along a relationship), the copy takes its place, with the copy's
      columns in the join's ON clause;
    - a subquery that the statement selects from only through its columns, criteria or entities gets
      its copy added to the FROM list, which SQLAlchemy reads before them. So does the left side of a
      join along a relationship (``join_from(alias, alias.rel)``), which SQLAlchemy finds there. A
      FROM list changes how SQLAlchemy picks the left side of a join that does not give one, so such
      joins get the one SQLAlchemy chose for ``stmt``, from ``join_lefts``;
    - a SELECT nested in the statement (a scalar subquery, ``IN``, ``EXISTS``) that selects from a
      subquery gets its copy too, as a statement of its own.
    """
    placed: set[int] = set()

    def copy_of(from_obj: Any) -> Any:
        if from_obj is None:
            return None
        key = id(_plain(_ungrouped(from_obj)))
        copy = copies.get(key)
        if copy is not None:
            placed.add(key)
        return copy

    def swap_from(from_obj: Any) -> Any:
        copy = copy_of(from_obj)
        if copy is not None:
            return copy
        if isinstance(_plain(_ungrouped(from_obj)), Join):
            # A join written with Core, which holds subqueries.
            return visitors.replacement_traverse(
                from_obj, {}, lambda element, **kw: copy_of(element) if _subquery_in(element) is not None else None
            )
        return from_obj

    def swap_columns(clause: Any) -> Any:
        return _on_copies(clause, copies)

    def swap_join(join: tuple[Any, ...]) -> tuple[Any, ...]:
        target, onclause, from_, flags = join
        # A join along a relationship goes to an entity: one over the copy takes the place of the
        # original (SQLAlchemy builds such an entity itself for a selectable, with a deprecation warning).
        if isinstance(target, QueryableAttribute) and onclause is None:
            # join(Parent.children): to the relationship's target, or its of_type() entity
            entity = _relationship_entity(target)
            copy = copy_of(entity.selectable) if entity is not None else None
            new_target, new_onclause = (target, onclause) if copy is None else (_entity_over(entity, copy), target)
        elif isinstance(onclause, QueryableAttribute):
            # join(entity, Parent.children): to the given entity
            copy = copy_of(target)
            entity = _annotated_entity(target) or _relationship_entity(onclause)
            new_target, new_onclause = (target, onclause) if copy is None else (_entity_over(entity, copy), onclause)
        else:
            new_target, new_onclause = swap_from(target), place_in_nested(onclause)
        if isinstance(new_onclause, QueryableAttribute) or isinstance(target, QueryableAttribute):
            # SQLAlchemy joins along a relationship from the attribute's own entity, which it looks for
            # in the FROM list: the copy of a left side that is a subquery goes there.
            new_from = (
                None
                if from_ is not None
                and _plain(_ungrouped(from_)) is not None
                and id(_plain(_ungrouped(from_))) in copies
                else from_
            )
        else:
            new_from = swap_from(join_lefts.get(id(join), from_))
        if new_target is target and new_onclause is onclause and new_from is from_:
            return join
        if new_onclause is not None and not isinstance(new_onclause, QueryableAttribute):
            # Columns of the original would let SQLAlchemy join from the original as well.
            new_onclause = swap_columns(new_onclause)
        return (new_target, new_onclause, new_from, flags)

    def swap_joins(joins: tuple[Any, ...]) -> tuple[Any, ...]:
        swapped = tuple(swap_join(join) for join in joins)
        return joins if all(new is old for new, old in zip(swapped, joins)) else swapped

    def place_in_nested(clause: Any) -> Any:
        """``clause``, with the SELECTs nested in it that select from a copied subquery selecting from
        the copy; ``clause`` itself when there are none (columns keep their identity)."""
        if clause is None or not any(_selects_from_copies(child, copies) for child in visitors.iterate(clause)):
            return clause

        def replace(element: Any, **kw: Any) -> Any:
            if _selects_from_copies(element, copies):
                return _place_copies(element, copies, {})
            return None

        return visitors.replacement_traverse(clause, {}, replace)

    def place_in_all(elements: Any) -> Any:
        placed_elements = tuple(place_in_nested(element) for element in elements)
        return elements if all(new is old for new, old in zip(placed_elements, elements)) else placed_elements

    # The new statement is this function's own copy: setting its attributes modifies nothing else.
    rewritten = stmt._generate()
    rewritten._raw_columns = list(place_in_all(stmt._raw_columns))
    rewritten._where_criteria = place_in_all(stmt._where_criteria)
    rewritten._having_criteria = place_in_all(stmt._having_criteria)
    rewritten._order_by_clauses = place_in_all(stmt._order_by_clauses)
    rewritten._group_by_clauses = place_in_all(stmt._group_by_clauses)
    rewritten._from_obj = tuple(swap_from(from_obj) for from_obj in stmt._from_obj)
    rewritten._setup_joins = swap_joins(stmt._setup_joins)
    if stmt._memoized_select_entities:
        # with_only_columns() sets the statement's earlier joins aside.
        memoized = []
        for entities in stmt._memoized_select_entities:
            joins = swap_joins(entities._setup_joins)
            if joins is not entities._setup_joins:
                entities = entities._clone()
                entities._setup_joins = joins
            memoized.append(entities)
        rewritten._memoized_select_entities = tuple(memoized)
    unplaced = tuple(copy for key, copy in copies.items() if key not in placed)
    if unplaced:
        rewritten._from_obj = rewritten._from_obj + unplaced
    return rewritten


def _selects_from_copies(element: Any, copies: dict[int, FromClause]) -> bool:
    """Whether ``element`` is a SELECT that selects from (or refers to columns of) a subquery copied in
    ``copies``."""
    if not isinstance(element, Select):
        return False
    for child in visitors.iterate(element):
        table = child.table if isinstance(child, ColumnClause) else child if isinstance(child, FromClause) else None
        if table is not None and id(_plain(_ungrouped(table))) in copies:
            return True
    return False


def _relationship_entity(attr: QueryableAttribute[Any]) -> Any:
    """The entity a join along a relationship attribute joins to: its ``of_type()`` entity, or the
    relationship's own target (a class, or an ``aliased()`` class)."""
    prop = attr.property
    if not isinstance(prop, RelationshipProperty):
        return None
    entity: Any = attr._of_type if attr._of_type is not None else prop.entity
    return inspect(entity)


def _relationship_target(attr: QueryableAttribute[Any]) -> Any:
    """The selectable a join along a relationship attribute joins to."""
    entity = _relationship_entity(attr)
    return entity.selectable if entity is not None else None


def _annotated_entity(element: Any) -> Any:
    """The entity (mapper or ``aliased()`` entity) an ORM-annotated element belongs to, if any."""
    return element._annotations.get("parententity") if isinstance(element, Annotated) else None


def _entity_over(insp: Any, selectable: FromClause) -> Any:
    """A join target like the entity ``insp`` (a mapper, or an ``aliased()`` entity), over ``selectable``:
    an ``aliased()`` entity with the same settings."""
    if isinstance(insp, AliasedInsp):
        entity = AliasedClass(
            insp.mapper,
            alias=selectable,
            name=insp.name,
            with_polymorphic_mappers=insp.with_polymorphic_mappers if insp._is_with_polymorphic else (),
            with_polymorphic_discriminator=insp.polymorphic_on,
            adapt_on_names=insp._adapt_on_names,
            use_mapper_path=insp._use_mapper_path,
            represents_outer_join=insp.represents_outer_join,
        )
    else:
        entity = AliasedClass(insp.mapper, alias=selectable)
    return inspect(entity).__clause_element__()


def _plain(element: Any) -> Any:
    """``element`` without the ORM's annotations: the object the statement was built with."""
    return element._deannotate() if isinstance(element, Annotated) else element


def _cte_root(selectable: Any) -> Any:
    """The CTE an alias of a CTE (``cte.alias()``) refers to, through any number of aliases; ``selectable``
    itself otherwise."""
    while isinstance(selectable, CTE) and selectable._cte_alias is not None:
        selectable = selectable._cte_alias
    return selectable


def _subquery_in(selectable: Any) -> Subquery | CTE | None:
    """The subquery (or CTE) ``selectable`` is, or wraps in aliases, LATERAL and parentheses, if any."""
    while True:
        if isinstance(selectable, (Subquery, CTE)):
            return selectable
        if isinstance(selectable, (Alias, Lateral, FromGrouping)):
            selectable = selectable.element
            continue
        return None


def _selects_from_mapper(element: Any, mapper: Mapper[Any]) -> bool:
    """Whether an ORM element selects from its mapper's own tables (its entity, or one of its columns),
    rather than from a subquery whose columns carry the mapper's annotations."""
    table = element if isinstance(element, FromClause) else getattr(element, "table", None)
    if table is None:
        return False
    table = _plain(table)
    return any(table is mapped for mapped in mapper.tables)


def _ungrouped(from_obj: Any) -> Any:
    """The FROM element inside parentheses (a JOIN nested on the right of another JOIN is grouped)."""
    while isinstance(from_obj, FromGrouping):
        from_obj = from_obj.element
    return from_obj


def _copy_selectable(selectable: Any, element: Any) -> Any:
    """A copy of a subquery or alias (same name, so the same SQL) around a different element, which
    SQLAlchemy knows as a clone of the original."""
    copy = selectable._clone()
    # _clone() makes a clone of a clone a clone of the first object: the copy must be one of the
    # caller's own object, whose columns the statement selects, for SQLAlchemy to leave that one out.
    copy._is_clone_of = selectable
    copy.element = element
    copy._reset_column_collection()
    return copy


class _LambdaRewriteToken:
    """Marks, in a ``lambda_stmt`` cache key, the statements one rewriter has rewritten."""

    __slots__ = ("field_name",)

    def __init__(self, field_name: str) -> None:
        self.field_name = field_name

    def __repr__(self) -> str:
        return f"<soft-delete rewrite of {self.field_name!r} at {id(self):#x}>"


_CRITERIA_TEMPLATE = (lambda cls: cls._soft_delete_field_.is_(None)).__code__
_CRITERIA_PREFIX = "_is_not_soft_deleted_"
_criteria_functions: dict[str, Callable[[Any], Any]] = {}


class _SoftDeleteCriteria(LoaderCriteriaOption):
    """``with_loader_criteria()`` for the soft-delete filter of eager loading, which SQLAlchemy applies to every
    SELECT of the class compiled with the statement. It leaves out the SELECTs in ``disabled`` (by id): the
    statements of subqueries in the FROM clause (subqueries, CTEs, LATERAL subqueries), or in that of a
    subquery in it, that disable soft-delete filtering, and the SELECTs nested in them, such as a subquery that
    counts soft-deleted rows on purpose.

    Only those: a statement of the same shape without the option gets a filtered copy of such a subquery,
    so the two have different cache keys and never share compiled SQL. A SELECT nested elsewhere (a scalar
    subquery) is not copied, and its execution options are left out of cache keys: it stays filtered.
    ``disabled`` holds objects of one statement and is not part of the cache key.
    """

    _traverse_internals = [
        *LoaderCriteriaOption._traverse_internals,
        ("disable_option_name", InternalTraversal.dp_string),
    ]

    def __init__(
        self,
        entity: Any,
        where_criteria: Any,
        *,
        include_aliases: bool,
        disable_option_name: str,
        disabled: frozenset[int] = frozenset(),
        configuration: tuple[str, str, tuple[tuple[str, str | None], ...]] | None = None,
    ) -> None:
        super().__init__(entity, where_criteria, include_aliases=include_aliases)
        self.disable_option_name = disable_option_name
        self.disabled = disabled
        self.configuration = configuration

    def _should_include(self, compile_state: Any) -> bool:
        # SQLAlchemy compiles a copy of a SELECT nested in the statement, which refers to the SELECT it copies
        # (or the first of its clones) through _is_clone_of.
        if self.disabled:
            statement = compile_state.select_statement
            while statement is not None:
                if id(statement) in self.disabled:
                    return False
                statement = getattr(statement, "_is_clone_of", None)
        return bool(super()._should_include(compile_state))

    def __reduce__(self) -> Any:
        # LoaderCriteriaOption's own rebuilds a plain LoaderCriteriaOption. The SELECTs of ``disabled`` belong to
        # the statement that loaded the instance.
        entity = self.entity.class_ if self.entity else self.root_entity
        if self.configuration is not None:
            # The condition of a class mapped to a SELECT is on its mapped attributes, which refer to its mapper:
            # it is made again, by a rewriter of the same configuration.
            return (
                _unpickle_mapped_subquery_criteria,
                (entity, self.include_aliases, self.configuration),
            )
        return (
            _unpickle_soft_delete_criteria,
            (entity, self._where_crit_orig, self.include_aliases, self.disable_option_name),
        )


_unpickling_rewriters: dict[Any, SoftDeleteQueryRewriter] = {}


def _unpickle_mapped_subquery_criteria(
    entity: Any, include_aliases: bool, configuration: tuple[str, str, tuple[tuple[str, str | None], ...]]
) -> _SoftDeleteCriteria:
    rewriter = _unpickling_rewriters.get(configuration)
    if rewriter is None:
        field_name, option_name, ignored = configuration
        rewriter = SoftDeleteQueryRewriter(
            field_name, option_name, [IgnoredTable(name, schema) for name, schema in ignored], rewrite_cache_size=0
        )
        rewriter = _unpickling_rewriters.setdefault(configuration, rewriter)
    return _SoftDeleteCriteria(
        entity,
        rewriter._mapped_subquery_condition(entity),
        include_aliases=include_aliases,
        disable_option_name=configuration[1],
        configuration=configuration,
    )


def _unpickle_soft_delete_criteria(
    entity: Any, where_criteria: Any, include_aliases: bool, disable_option_name: str
) -> _SoftDeleteCriteria:
    return _SoftDeleteCriteria(
        entity, where_criteria, include_aliases=include_aliases, disable_option_name=disable_option_name
    )


def _is_null_criteria(field_name: str) -> Callable[[Any], Any]:
    """``lambda cls: cls.<field_name>.is_(None)``, for ``with_loader_criteria``.

    SQLAlchemy calls the lambda with each entity and each ``aliased()`` form of it, so the filter is
    built against the alias itself. A plain expression would have to be adapted to the alias, which
    SQLAlchemy does not do for an explicit ``join(alias, onclause)`` and renders invalid SQL.

    The field name cannot be a closure variable: SQLAlchemy replaces those with trackers while it
    analyzes the lambda, and caches the analyzed lambda by its code object, so a default argument
    would leak one configuration's field name into another's. The name is written into the code
    object instead, which gives each field name its own code, and so its own cache entries. Each field
    name has a single function, named after it, which pickle can find again (see ``__getattr__``).
    """
    name = _CRITERIA_PREFIX + field_name.encode().hex()
    function = _criteria_functions.get(name)
    if function is None:
        code = _CRITERIA_TEMPLATE.replace(
            co_names=tuple(field_name if n == "_soft_delete_field_" else n for n in _CRITERIA_TEMPLATE.co_names)
        )
        candidate = types.FunctionType(code, {}, name)
        candidate.__qualname__ = name
        candidate.__module__ = __name__
        function = _criteria_functions.setdefault(name, candidate)
    return function


def __getattr__(name: str) -> Any:
    """Find the criteria functions by name, as pickle does.

    Instances keep the loader criteria of the query that loaded them, for their lazy loads, so pickling
    an instance pickles them, and pickle stores a function as its module and name.
    """
    if name.startswith(_CRITERIA_PREFIX):
        try:
            field_name = bytes.fromhex(name[len(_CRITERIA_PREFIX) :]).decode()
        except ValueError:
            pass
        else:
            return _is_null_criteria(field_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
