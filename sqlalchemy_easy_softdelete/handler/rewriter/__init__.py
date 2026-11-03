"""Main query rewriter logic."""

from __future__ import annotations

import itertools
import threading
import types
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple, cast

from sqlalchemy import Column, Table, inspect
from sqlalchemy.orm import FromStatement, Mapper, RelationshipProperty, with_loader_criteria
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.orm.strategy_options import Load
from sqlalchemy.orm.util import AliasedClass, AliasedInsp
from sqlalchemy.sql import Alias, CompoundSelect, Executable, Join, Select, Subquery, TableClause, visitors
from sqlalchemy.sql import util as sql_util
from sqlalchemy.sql.annotation import Annotated
from sqlalchemy.sql.elements import ColumnClause, TextClause
from sqlalchemy.sql.lambdas import LambdaElement
from sqlalchemy.sql.selectable import (
    AliasedReturnsRows,
    FromClause,
    FromGrouping,
    SelectBase,
    SelectStatementGrouping,
    Values,
)

from sqlalchemy_easy_softdelete.hook import IgnoredTable

# Type alias for statements we can rewrite
Statement = Select | FromStatement | CompoundSelect | Executable | LambdaElement

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
            # Explicitly protect against INSERT with RETURNING
            if not isinstance(stmt.element, Select) or self._is_disabled(stmt):
                return stmt
            # The element is what SQLAlchemy compiles: loader criteria apply from there.
            element = self.rewrite_select(stmt.element)
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
        rewritten_resolved: Select[Any] | CompoundSelect
        if isinstance(resolved, Select):
            rewritten_resolved = self.rewrite_select(resolved)
        elif isinstance(resolved, CompoundSelect):
            rewritten_resolved = self.rewrite_compound_select(resolved)
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
        return self.rewrite_select(cast(Select[Any], member))

    def _is_disabled(self, stmt: Any) -> bool:
        """Whether the statement disables soft-delete filtering with the configured execution option."""
        return bool(stmt.get_execution_options().get(self.disable_soft_delete_option_name))

    def _with_criteria(self, stmt: Any, criteria: _Criteria) -> Any:
        """``stmt`` with ``with_loader_criteria`` options for ``criteria``."""
        if not criteria:
            return stmt
        return stmt.options(
            *(
                with_loader_criteria(entity, self._is_not_deleted, include_aliases=include_aliases)
                for entity, include_aliases in criteria.items()
            )
        )

    def _plan_for(self, stmt: Select[Any]) -> tuple[_SelectPlan, _FromAnalysis | None]:
        """The plan for ``stmt``, a remembered one for its shape or a new one, and, for a new one, what
        the analysis of its FROM clause found.
        """
        cache = self._plan_cache
        cache_key = stmt._generate_cache_key() if cache is not None else None
        if cache is not None and cache_key is not None:
            plan = cache.get(cache_key.key)
            if plan is not None:
                return plan, None

        plan, analysis = self._plan_select(stmt)
        if cache is not None:
            if cache_key is not None and plan.reusable:
                cache.put(cache_key.key, plan)
            else:
                cache.count_uncacheable()
        return plan, analysis

    def _plan_select(self, stmt: Select[Any]) -> tuple[_SelectPlan, _FromAnalysis]:
        """Work out what the soft-delete rewrite adds to ``stmt``, and the subqueries of its FROM clause
        (or aliases of subqueries), which filtered copies replace.
        """
        columns: list[Column[Any]] = []
        from_subqueries: list[FromClause] = []
        final_froms = _final_froms_without_loader_options(stmt)
        for from_obj in final_froms:
            self._collect_from(from_obj, columns, from_subqueries)

        # Eager loading joins aliases of the loaded classes: their filter applies to aliased() forms.
        eager_entities, reusable = self._eager_loaded_entities(stmt)
        entities: _Criteria = dict.fromkeys(eager_entities, True)
        if entities and not self._keep_eager_criteria_off_unfilterable_aliases(stmt, entities):
            # It depends on execution options inside the statement, which cache keys leave out.
            reusable = False
        found: dict[int, FromClause] = {}
        for from_obj in from_subqueries:
            plain = _plain(from_obj)
            found.setdefault(id(plain), plain)

        subqueries: list[FromClause] = []
        join_lefts: dict[int, Any] = {}
        if found:
            scan = _FromScan(stmt)
            if scan.subqueries.keys() != found.keys():
                # A later statement of this shape would not find its subqueries without this analysis.
                reusable = False
            # Only the subqueries the statement names can be replaced; SQLAlchemy builds the others
            # (an alias of a relationship's secondary) while it compiles the statement.
            subqueries = [subquery for key, subquery in found.items() if key in scan.subqueries]
            if any(id(subquery) not in scan.explicit for subquery in subqueries):
                # Copies of subqueries the statement does not name as a FROM join its FROM list.
                join_lefts = _implicit_join_lefts(stmt, final_froms)
                if join_lefts:
                    reusable = False

        plan = _SelectPlan(
            tuple(columns),
            tuple(entities.items()),
            rewrites_subqueries=bool(subqueries),
            reusable=reusable,
        )
        return plan, _FromAnalysis(subqueries, join_lefts)

    def _keep_eager_criteria_off_unfilterable_aliases(self, stmt: Select[Any], entities: _Criteria) -> bool:
        """Turn off, in ``entities``, the eager loading criteria's reach into the ``aliased()`` forms of
        each class that has, anywhere in ``stmt``, an ``aliased()`` entity over a subquery they must not
        filter: one whose statement includes soft-deleted rows, or one that does not pass on the
        soft-delete column (SQLAlchemy would then filter the class's table, which the statement does not
        select from there). Return whether the result holds for every statement of the same shape: it
        does not when such entities are over subqueries, whose execution options cache keys leave out.
        """
        reusable = True
        seen: set[int] = set()
        for element in visitors.iterate(stmt):
            insp = _annotated_entity(element)
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
        if plan.rewrites_subqueries:
            if analysis is None:
                analysis = _FromAnalysis(list(_FromScan(stmt).subqueries.values()), {})
            memo: dict[int, FromClause | None] = {}
            copies: dict[int, FromClause] = {}
            for from_obj in analysis.subqueries:
                copy = self._filtered_copy(from_obj, memo)
                if copy is not None:
                    copies[id(from_obj)] = copy
            if copies:
                stmt = _place_copies(stmt, copies, analysis.join_lefts)

        stmt = self._with_criteria(stmt, dict(plan.criteria_entities))

        for column in plan.filter_columns:
            stmt = stmt.filter(column.is_(None))
        return stmt

    def _filtered_copy(self, from_obj: FromClause, memo: dict[int, FromClause | None]) -> FromClause | None:
        """A copy of a subquery (or of an alias of one) around its rewritten statement, or None when the
        rewrite leaves the statement as it is. ``memo`` keeps the copies made for one statement."""
        key = id(from_obj)
        if key not in memo:
            element: Any = cast(AliasedReturnsRows, from_obj).element
            inner: Any
            if isinstance(from_obj, Subquery):
                inner = self._rewrite_subquery_element(element)
                unchanged = inner is element
            else:
                # An alias of a subquery (an alias of an aliased() entity, or of a subquery's alias)
                inner = self._filtered_copy(element, memo)
                unchanged = inner is None
            memo[key] = None if unchanged else _copy_selectable(from_obj, inner)
        return memo[key]

    def _collect_from(self, from_obj: Any, columns: list[Column[Any]], subqueries: list[FromClause]) -> None:
        """Collect the soft-delete columns and subqueries of one element of a statement's FROM list."""
        if isinstance(from_obj, Table):
            self._collect_table(from_obj, columns)
            return

        if isinstance(from_obj, Join):
            # Join (and the ORM's _ORMJoin) holds two things: 'left' and 'right'. Check both.
            self._collect_join(from_obj, columns, subqueries)
            return

        if isinstance(from_obj, FromGrouping):
            # A parenthesized FROM element, such as a JOIN nested on the right of another JOIN.
            self._collect_from(from_obj.element, columns, subqueries)
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

        if isinstance(from_obj, Alias):
            raise NotImplementedError(
                f'Unsupported object "{(type(from_obj.element))}" inside Alias in statement.froms'
            )

        raise NotImplementedError(f'Unsupported object "{(type(from_obj))}" in statement.froms')

    def _collect_join(self, join: Join, columns: list[Column[Any]], subqueries: list[FromClause]) -> None:
        """Handle multiple, and potentially recursive joins: nested joins, then subqueries, then tables."""
        sides = (_ungrouped(join.left), _ungrouped(join.right))
        for side in sides:
            if isinstance(side, Join):
                self._collect_join(side, columns, subqueries)
        for side in sides:
            # A subquery, or an alias of one (an alias of an aliased() entity).
            if not isinstance(side, Join) and _subquery_in(side) is not None:
                subqueries.append(side)
        for side in sides:
            if isinstance(side, Table):
                self._collect_table(side, columns)

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
            for path in [loader.path for loader in opt.context] or [opt.path]:
                # PathRegistry is iterable at runtime but not typed as such
                path_elements: list[Any] = list(path) if path is not None else []  # type: ignore[call-overload]
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
                    if entity_class is not None and self._is_soft_deletable_entity(entity_class):
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


def _joins_of(stmt: Select[Any]) -> Any:
    """The joins of ``stmt``, including those ``with_only_columns()`` set aside."""
    return itertools.chain(stmt._setup_joins, *(entities._setup_joins for entities in stmt._memoized_select_entities))


def _implicit_join_lefts(stmt: Select[Any], final_froms: Any) -> dict[int, Any]:
    """The left side SQLAlchemy chose in ``final_froms`` (the analyzed FROM clause of ``stmt``) for each
    join of ``stmt`` that does not give one (``join(Target)``, ``join(Target, onclause)``), by the id of
    the join: the FROM element the join attaches to, as SQLAlchemy finds it among those of its left."""
    joins_by_right: dict[int, Join] = {}
    stack = list(final_froms)
    while stack:
        from_obj = _ungrouped(stack.pop())
        if isinstance(from_obj, Join):
            joins_by_right.setdefault(id(_plain(_ungrouped(from_obj.right))), from_obj)
            stack.extend((from_obj.left, from_obj.right))

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
        def replace(element: Any, **kw: Any) -> Any:
            table = element.table if isinstance(element, ColumnClause) else None
            copy = copies.get(id(_plain(table))) if table is not None else None
            return copy.c[element.key] if copy is not None else None

        return visitors.replacement_traverse(clause, {}, replace)

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


def _subquery_in(selectable: Any) -> Subquery | None:
    """The subquery ``selectable`` is, or wraps in aliases and parentheses, if any."""
    while True:
        if isinstance(selectable, Subquery):
            return selectable
        if isinstance(selectable, (Alias, FromGrouping)):
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
