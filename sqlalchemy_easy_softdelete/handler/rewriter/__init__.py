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
from sqlalchemy.orm import Bundle, FromStatement, Mapper, RelationshipProperty, with_loader_criteria
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.orm.strategy_options import Load
from sqlalchemy.orm.util import AliasedClass, AliasedInsp
from sqlalchemy.sql import Alias, CompoundSelect, Executable, Join, Select, Subquery, TableClause, visitors
from sqlalchemy.sql.annotation import Annotated
from sqlalchemy.sql.elements import ColumnClause, Label, TextClause
from sqlalchemy.sql.lambdas import LambdaElement
from sqlalchemy.sql.selectable import FromClause, FromGrouping, SelectBase, SelectStatementGrouping, Values
from sqlalchemy.util import immutabledict

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
    without a cache key, and statements that select from a subquery through an ``aliased()`` entity."""

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
    """Soft-delete columns of the tables in the FROM clause, filtered with ``IS NULL``, and of the
    subqueries in it that stay as they are (a class's mapped subquery, an ``aliased()`` subquery that
    cannot be replaced)."""

    criteria_entities: tuple[tuple[type, bool], ...]
    """Mapped classes filtered through ``with_loader_criteria`` options on this statement (eager loading),
    each with whether the filter also applies to the class's ``aliased()`` forms."""

    compile_time_entities: tuple[type, ...]
    """Mapped classes filtered while SQLAlchemy compiles this statement, for subqueries that must stay as
    they are: the statement registers loader criteria when it is compiled, which reach the SELECTs
    nested in it. Loader criteria options would only apply on the statement that is executed."""

    rewrites_subqueries: bool
    """Whether subqueries the statement holds in its FROM clause are replaced by rewritten copies."""

    reusable: bool
    """Whether the plan holds for every statement with the same cache key.

    It does not when a subquery is reached through an ``aliased()`` entity, or not referred to at all:
    the plan then depends on objects of that statement, or on execution options of the statement
    inside the subquery, which cache keys leave out. Nor when the subqueries a statement holds cannot
    be found again without analyzing its FROM clause.
    """


class _FromAnalysis(NamedTuple):
    """The subqueries of one statement that its plan replaces, from the analysis of its FROM clause."""

    held: list[Subquery]
    """Subqueries the statement holds itself."""

    aliased: list[tuple[Subquery, list[AliasedInsp[Any]]]]
    """Subqueries reached through ``aliased()`` entities, with those entities."""

    filtered_aliases: list[tuple[int, AliasedInsp[Any]]] = []
    """``aliased()`` entities over subqueries that stay as they are, on the outer side of a LEFT OUTER or
    FULL JOIN, whose rows are filtered in the join while SQLAlchemy compiles the statement, each with
    its position among the subqueries of the statement's FROM clause."""


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


class _StatementReferences:
    """How a statement refers to the subqueries in its FROM clause.

    A subquery the statement holds directly (``join(sq)``, ``select(sq.c.x)``, ``select_from(sq)``) is
    replaced by a rewritten copy. So is a subquery the ORM reaches through an ``aliased()`` entity,
    which keeps pointing at the original object: a new ``aliased()`` entity over the copy takes the
    place of the old one wherever the statement refers to it. When the entity is also referred to
    where it cannot be replaced (loader options, relationship joins of a SELECT nested in the
    statement, joins and options that ``with_only_columns()`` set aside, a ``Bundle``, an alias of
    the entity, ``with_polymorphic()``), the subquery is "fixed": it stays as it is, and is filtered
    from outside instead (see ``SoftDeleteQueryRewriter._filter_fixed_subquery``). So is a subquery
    that is the selectable of a mapped class (a class mapped to a SELECT, a concrete inheritance
    union), and one the statement does not refer to at all.
    """

    def __init__(self, stmt: Select[Any]) -> None:
        self.held: set[int] = set()
        self.aliases: dict[int, list[AliasedInsp[Any]]] = {}
        self.fixed: set[int] = set()
        self.mappers: dict[int, list[Mapper[Any]]] = {}

        for element in visitors.iterate(stmt):
            if isinstance(element, Annotated):
                self._note(element._annotations.get("parententity"))
                bundle = element._annotations.get("bundle")
                if isinstance(bundle, Bundle):
                    # The ORM renders a Bundle from its own expressions, which no replacement reaches.
                    self._note_bundle(bundle)
            else:
                self.held.add(id(element))
            if not isinstance(element, Select):
                continue
            # Relationship joins (`join(Parent.children.of_type(alias))`) hold attributes, which the
            # traversal does not reach. Only those of the statement itself are rebuilt.
            self._note_joins_and_options(element, fixed_joins=element is not stmt)
            # with_only_columns() sets the statement's earlier joins and options aside.
            for memoized in getattr(element, "_memoized_select_entities", ()):
                self._note_joins_and_options(memoized, fixed_joins=True)

    def _note_joins_and_options(self, element: Any, fixed_joins: bool) -> None:
        for part in _relationship_join_parts(element):
            self._note(part.parent, fixed=fixed_joins)
            self._note(part._of_type, fixed=fixed_joins)
        for option in element._with_options:
            if isinstance(option, Load):
                for path_element in _loader_path_elements(option):
                    self._note(getattr(path_element, "parent", path_element), fixed=True)
                    self._note(getattr(path_element, "_of_type", None), fixed=True)

    def _note_bundle(self, bundle: Bundle[Any]) -> None:
        for expr in bundle.exprs:
            if isinstance(expr, Bundle):
                self._note_bundle(expr)
            else:
                self._note(getattr(expr, "_annotations", {}).get("parententity"), fixed=True)

    def _note(self, entity: Any, fixed: bool = False) -> None:
        insp = inspect(entity, raiseerr=False) if entity is not None else None
        if isinstance(insp, Mapper):
            for selectable in _mapper_selectables(insp):
                subquery = _subquery_in(selectable)
                if subquery is not None:
                    mappers = self.mappers.setdefault(id(subquery), [])
                    if insp not in mappers:
                        mappers.append(insp)
            return
        if not isinstance(insp, AliasedInsp):
            return
        self._register(id(insp.selectable), insp, fixed or not _can_rebuild(insp))
        inner = _subquery_in(insp.selectable)
        if inner is not None and inner is not insp.selectable:
            # An alias of an aliased() entity selects from an Alias of the subquery, inside which a
            # rewritten copy cannot be placed.
            self._register(id(inner), insp, fixed=True)

    def _register(self, key: int, insp: AliasedInsp[Any], fixed: bool) -> None:
        insps = self.aliases.setdefault(key, [])
        if not any(known is insp for known in insps):
            insps.append(insp)
        if fixed:
            self.fixed.add(key)


class SoftDeleteQueryRewriter:
    """Rewrites SQL statements based on configuration.

    Rewriting never modifies the statement it is given, nor any object inside it: it returns a new
    statement. Applications can therefore reuse statements, subqueries and ``lambda_stmt`` objects
    across executions, with and without soft-delete filtering.

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

    def _with_compile_time_criteria(
        self, stmt: Select[Any], entities: tuple[type, ...], aliases: list[tuple[int, AliasedInsp[Any]]]
    ) -> Select[Any]:
        """``stmt``, registering loader criteria for ``entities`` and for the ``aliased()`` entities of
        ``aliases`` when SQLAlchemy compiles it.

        SQLAlchemy applies the loader criteria options of the statement it executes only (on 1.4, only
        those of an ORM statement), to it and to the SELECTs nested in it. A statement's context
        options, on the other hand, run whenever SQLAlchemy compiles it, at any depth, before the
        SELECTs nested in it. The criteria they register reach those SELECTs, and the SELECTs compiled
        after it. Criteria of an ``aliased()`` entity apply to that entity only, in the ON clause of a
        join to it (so a LEFT OUTER JOIN keeps its rows without a match), otherwise in the WHERE clause.
        """
        options = [with_loader_criteria(entity, self._is_not_deleted) for entity in entities]
        options.extend(with_loader_criteria(insp.entity, self._is_not_deleted) for _, insp in aliases)

        def register_criteria(compile_state: Any) -> None:
            for option in options:
                option.process_compile_state(compile_state)

        # SQLAlchemy keys the compiled statement on the function's code and on these arguments: the
        # entities filtered, and where the filtered aliases are (which statements of the same shape do
        # not filter when the statement inside includes soft-deleted rows).
        positions = tuple(position for position, _ in aliases)
        return stmt._add_context_option(register_criteria, (self.deleted_field_name, entities, positions))

    def _plan_for(self, stmt: Select[Any]) -> tuple[_SelectPlan, _FromAnalysis | None]:
        """The plan for ``stmt``, a remembered one for its shape or a new one, and, for a new one, the
        subqueries of its FROM clause that the plan replaces.
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
        that it replaces.
        """
        columns: list[Column[Any]] = []
        subqueries: list[Subquery] = []
        outer_joined: set[int] = set()
        for from_obj in _final_froms_without_loader_options(stmt):
            self._collect_from(from_obj, columns, subqueries, outer_joined)

        # Eager loading joins aliases of the loaded classes: their filter applies to aliased() forms.
        entities: _Criteria = dict.fromkeys(self._eager_loaded_entities(stmt), True)
        compile_time_entities: dict[type, None] = {}
        filtered_aliases: list[tuple[int, AliasedInsp[Any]]] = []
        held: list[Subquery] = []
        aliased: dict[int, tuple[Subquery, list[AliasedInsp[Any]]]] = {}
        reusable = True
        references = _StatementReferences(stmt) if subqueries else None
        for position, subquery in enumerate(dict.fromkeys(subqueries)):
            assert references is not None
            selectable: Subquery = subquery
            insps = references.aliases.get(id(subquery))
            if insps is None and isinstance(subquery, Annotated):
                # An explicit join to an aliased() entity, or select_from() of an entity, puts the
                # entity's annotated subquery in the FROM clause, rather than the subquery itself.
                insp = subquery._annotations.get("parententity")
                if isinstance(insp, AliasedInsp):
                    selectable = cast(Subquery, insp.selectable)
                    insps = references.aliases.get(id(selectable), [insp])
                elif isinstance(insp, Mapper):
                    selectable = cast(Subquery, subquery._deannotate())

            if insps is None and id(subquery) in references.held:
                held.append(subquery)
                continue

            if insps is None and id(selectable) in references.mappers:
                # The selectable of a mapped class (a class mapped to a SELECT, a concrete inheritance
                # union): part of the mapping, the same object in every statement, which cannot be
                # replaced. It is filtered like a table, on its own soft-delete column (a union selects
                # each table's), unless the statement inside it includes soft-deleted rows.
                if self._filtered_mappers_in(selectable.element)[1]:
                    self._collect_table(selectable, columns)
                continue

            reusable = False
            if insps is not None and entities.keys() & {insp.mapper.class_ for insp in insps}:
                if not self._filtered_mappers_in(selectable.element)[1]:
                    # The statement inside includes soft-deleted rows: the eager loading criteria of
                    # its class must not filter the entity's rows either.
                    for insp in insps:
                        if insp.mapper.class_ in entities:
                            entities[insp.mapper.class_] = False

            if insps is not None and id(selectable) not in references.fixed and all(map(_can_rebuild, insps)):
                aliased.setdefault(id(selectable), (selectable, insps))
                continue

            # The subquery must stay as it is.
            is_outer_joined = id(subquery) in outer_joined
            for insp, column in self._filter_fixed_subquery(
                selectable, insps or [], is_outer_joined, compile_time_entities
            ):
                if is_outer_joined:
                    # Filtering the WHERE clause would drop the rows that a LEFT OUTER JOIN keeps
                    # without a match: the entity's own criteria filter the join instead.
                    filtered_aliases.append((position, insp))
                else:
                    columns.append(column)

        if held and {id(subquery) for subquery in _held_from_subqueries(stmt)} != {id(s) for s in held}:
            # A later statement of this shape would not find its subqueries without this analysis.
            reusable = False

        plan = _SelectPlan(
            tuple(columns),
            tuple(entities.items()),
            tuple(compile_time_entities),
            rewrites_subqueries=bool(held),
            reusable=reusable,
        )
        return plan, _FromAnalysis(held, list(aliased.values()), filtered_aliases)

    def _filter_fixed_subquery(
        self,
        subquery: Subquery,
        insps: list[AliasedInsp[Any]],
        is_outer_joined: bool,
        compile_time_entities: dict[type, None],
    ) -> list[tuple[AliasedInsp[Any], Any]]:
        """Filter a subquery that must stay as it is, at compile time (see ``_with_compile_time_criteria``),
        or return the ``aliased()`` entities over it whose rows to filter, each with the soft-delete
        column its subquery passes on.

        ``insps`` are the ``aliased()`` entities over the subquery. When the statement inside selects
        rows of one table and nothing else, filtering the entities' rows on that column keeps the rows
        that filtering inside would. Otherwise the entities that the statement inside refers to are
        filtered, wherever SQLAlchemy selects from them. So are they on the outer side of an outer join,
        where filtering the entities' rows takes the join's ON clause, so loader criteria, which
        SQLAlchemy 2.0.10 does not apply to a join with an ON clause.
        """
        element = subquery.element
        if isinstance(element, (Select, CompoundSelect)) and self._is_disabled(element):
            return []
        if insps and not is_outer_joined and _selects_rows_of_one_table(element):
            filterable = self._filterable_aliases(insps)
            if filterable is not None:
                return filterable

        mappers, fully_filtered = self._filtered_mappers_in(element)
        for mapper in mappers:
            if self._is_soft_deletable_entity(mapper.class_):
                compile_time_entities.setdefault(mapper.class_, None)
        if not fully_filtered:
            return []
        # A Core statement inside refers to no entity: filter the rows of the entities over it.
        inner_classes = {mapper.class_ for mapper in mappers}
        return self._filterable_aliases([insp for insp in insps if insp.mapper.class_ not in inner_classes]) or []

    def _filterable_aliases(self, insps: list[AliasedInsp[Any]]) -> list[tuple[AliasedInsp[Any], Any]] | None:
        """The soft-deletable entities of ``insps``, each with the soft-delete column its subquery passes on,
        or None when the subquery of one of them does not pass it on."""
        found = []
        for insp in insps:
            if not self._is_soft_deletable_entity(insp.mapper.class_):
                continue
            column = insp.mapper.columns.get(self.deleted_field_name)
            passed_on = insp.selectable.corresponding_column(column) if column is not None else None
            if passed_on is None:
                return None
            found.append((insp, passed_on))
        return found

    def _apply_plan(self, stmt: Select[Any], plan: _SelectPlan, analysis: _FromAnalysis | None) -> Select[Any]:
        """Return a copy of ``stmt`` with ``plan`` applied.

        ``analysis`` holds the subqueries to replace when the FROM clause was just analyzed; otherwise
        the subqueries the statement holds are found again (a plan with aliased subqueries is never
        remembered).
        """
        if analysis is None:
            analysis = _FromAnalysis(_held_from_subqueries(stmt) if plan.rewrites_subqueries else [], [])

        copies: dict[int, Subquery] = {}
        aliases: dict[int, tuple[AliasedInsp[Any], AliasedInsp[Any]]] = {}
        for subquery in analysis.held:
            element = self._rewrite_subquery_element(subquery.element)
            if element is not subquery.element:
                copies[id(subquery)] = _copy_subquery(subquery, element)
        for subquery, insps in analysis.aliased:
            element = self._rewrite_subquery_element(subquery.element)
            if element is subquery.element:
                continue
            copy = _copy_subquery(subquery, element)
            copies[id(subquery)] = copy
            for insp in insps:
                aliases[id(insp)] = (insp, _alias_over(insp, copy))
        if copies:
            stmt = _replace_in_statement(stmt, copies, aliases)

        stmt = self._with_criteria(stmt, dict(plan.criteria_entities))
        filtered_aliases = analysis.filtered_aliases
        if plan.compile_time_entities or filtered_aliases:
            stmt = self._with_compile_time_criteria(stmt, plan.compile_time_entities, filtered_aliases)

        for column in plan.filter_columns:
            stmt = stmt.filter(column.is_(None))
        return stmt

    def _collect_from(
        self, from_obj: Any, columns: list[Column[Any]], subqueries: list[Subquery], outer_joined: set[int]
    ) -> None:
        """Collect the soft-delete columns and subqueries of one element of a statement's FROM list, and
        into ``outer_joined`` the ids of the subqueries on the outer side of a LEFT OUTER or FULL JOIN."""
        if isinstance(from_obj, Table):
            self._collect_table(from_obj, columns)
            return

        if isinstance(from_obj, Join):
            # Join (and the ORM's _ORMJoin) holds two things: 'left' and 'right'. Check both.
            self._collect_join(from_obj, columns, subqueries, outer_joined)
            return

        if isinstance(from_obj, Subquery):
            subqueries.append(from_obj)
            return

        if isinstance(from_obj, FromGrouping):
            # A parenthesized FROM element, such as a JOIN nested on the right of another JOIN.
            self._collect_from(from_obj.element, columns, subqueries, outer_joined)
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
            if isinstance(from_obj.element, Subquery):
                subqueries.append(from_obj.element)
                return

            raise NotImplementedError(
                f'Unsupported object "{(type(from_obj.element))}" inside Alias in statement.froms'
            )

        raise NotImplementedError(f'Unsupported object "{(type(from_obj))}" in statement.froms')

    def _collect_join(
        self,
        join: Join,
        columns: list[Column[Any]],
        subqueries: list[Subquery],
        outer_joined: set[int],
        outer_side: bool = False,
    ) -> None:
        """Handle multiple, and potentially recursive joins: nested joins, then subqueries, then tables.

        ``outer_side`` tells whether the join is on the outer side of a LEFT OUTER or FULL JOIN.
        """
        sides = (
            (_ungrouped(join.left), outer_side or join.full),
            (_ungrouped(join.right), outer_side or join.isouter or join.full),
        )
        for side, side_is_outer in sides:
            if isinstance(side, Join):
                self._collect_join(side, columns, subqueries, outer_joined, side_is_outer)
        for side, side_is_outer in sides:
            # A subquery, or an alias of one (an alias of an aliased() entity).
            subquery = side.element if isinstance(side, Alias) else side
            if isinstance(subquery, Subquery):
                subqueries.append(subquery)
                if side_is_outer:
                    outer_joined.add(id(subquery))
        for side, _ in sides:
            if isinstance(side, Table):
                self._collect_table(side, columns)

    def _collect_table(self, table: Table | Subquery, columns: list[Column[Any]]) -> None:
        """Collect the soft-delete column of ``table`` (or of a class's mapped subquery), unless it is
        ignored or has none."""
        if any(ignored.match_name(cast(Table, table)) for ignored in self.ignored_tables):
            return

        # Caveat: The automatic "bool(column_obj)" conversion actually returns a truthy value of False (?),
        # so we have to explicitly compare against None
        column_obj = table.columns.get(self.deleted_field_name)
        if column_obj is not None:
            columns.append(cast("Column[Any]", column_obj))

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

    def _eager_loaded_entities(self, stmt: Select[Any]) -> list[type]:
        """Soft-deletable entities named in the loader options' paths (joinedload, subqueryload, etc.).

        These are filtered with ``with_loader_criteria``, so the soft-delete filter also reaches the
        JOINed aliases and the separate queries that eager loading creates. Column loader options
        (``load_only``, ``defer``) name their entity too; their criteria repeat a filter the statement
        already has, which changes nothing.

        Handles both SQLAlchemy 1.4 and 2.0 structures:
        - 1.4: opt.path is a tuple of InstrumentedAttribute, entity via property.mapper.class_
        - 2.0: opt.path is SlotsEntityRegistry with Mapper objects, entity via class_
        """
        # Access _with_options - an internal attribute for ORM loader options.
        # This is part of SQLAlchemy's _traverse_internals (dp_executable_options),
        # meaning it's stable across versions. There is no public API to read options.
        entities: dict[type, None] = {}
        for opt in stmt._with_options:
            # Only process Load options (covers Load, _UnboundLoad in 1.4)
            if not isinstance(opt, Load):
                continue

            # Load.path is a public attribute containing the entity/relationship path
            path = opt.path
            if path is None:
                continue

            # PathRegistry is iterable at runtime but not typed as such
            path_elements: list[Any] = list(path)  # type: ignore[call-overload]
            for position, path_element in enumerate(path_elements):
                if position == 0 and isinstance(path_element, AliasedInsp):
                    # An aliased() entity the option starts from is in the statement's FROM clause,
                    # which the rewrite handles; the entities it loads are the ones to filter.
                    continue
                entity_class = self._extract_entity_class_from_path_element(path_element)
                if entity_class is not None and self._is_soft_deletable_entity(entity_class):
                    entities.setdefault(entity_class, None)

        return list(entities)

    def _is_soft_deletable_entity(self, entity_class: type) -> bool:
        """Whether ``entity_class`` has the soft-delete column and its table is not ignored."""
        if not hasattr(entity_class, self.deleted_field_name):
            return False

        table = getattr(entity_class, "__table__", None)
        if isinstance(table, Table) and any(ignored.match_name(table) for ignored in self.ignored_tables):
            return False

        return True

    def _extract_entity_class_from_path_element(self, path_element: Any) -> type | None:
        """Extract the entity class from a path element.

        Handles both SQLAlchemy 1.4 and 2.0 structures:
        - 1.4: InstrumentedAttribute (QueryableAttribute after of_type()) -> property.mapper.class_
        - 2.0: Mapper -> class_
        - an aliased() entity -> its mapper's class_
        """
        # SQLAlchemy 2.0: Mapper objects have class_ directly
        if isinstance(path_element, Mapper):
            return path_element.class_

        # An aliased() entity in the path (`joinedload(Parent.children.of_type(alias))`)
        if isinstance(path_element, AliasedInsp):
            return path_element.mapper.class_

        # SQLAlchemy 1.4: InstrumentedAttribute (QueryableAttribute for of_type()) -> property.mapper.class_
        if isinstance(path_element, QueryableAttribute):
            prop = path_element.property
            if isinstance(prop, RelationshipProperty):
                mapper = prop.mapper
                if isinstance(mapper, Mapper):
                    return mapper.class_

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


def _held_from_subqueries(stmt: Select[Any]) -> list[Subquery]:
    """The subqueries ``stmt`` itself puts in its FROM clause: ``select_from()``, join targets, and the
    subqueries its columns and WHERE criteria select from.

    This reads the statement without compiling it. A plan is only reused when, for the statement it
    was made from, this finds the same subqueries as the analysis of the FROM clause.
    """
    found: dict[int, Subquery] = {}

    def visit(from_obj: Any) -> None:
        from_obj = _ungrouped(from_obj)
        if isinstance(from_obj, Annotated):
            return
        if isinstance(from_obj, Subquery):
            found.setdefault(id(from_obj), from_obj)
        elif isinstance(from_obj, Alias):
            visit(from_obj.element)
        elif isinstance(from_obj, Join):
            visit(from_obj.left)
            visit(from_obj.right)

    joins = list(itertools.chain(stmt._setup_joins, getattr(stmt, "_legacy_setup_joins", ())))
    implied = itertools.chain.from_iterable(
        element._from_objects for element in itertools.chain(stmt._raw_columns, stmt._where_criteria)
    )
    for from_obj in itertools.chain(stmt._from_obj, (j[0] for j in joins), (j[2] for j in joins), implied):
        visit(from_obj)
    return list(found.values())


def _loader_path_elements(option: Load) -> list[Any]:
    """The entities and attributes along the paths of a loader option.

    An option holds a path for each attribute it loads (``load_only(a.x, a.y)``): SQLAlchemy 2.0 keeps
    them in the option's context, 1.4 keeps those of an option that is not bound to an entity in
    ``_to_bind``. Their elements are entities, attributes (1.4) or properties (2.0), and tokens.
    """
    loaders = [option, *getattr(option, "_to_bind", ())]
    context = getattr(option, "context", ())
    if isinstance(context, tuple):
        loaders.extend(context)
    elements: list[Any] = []
    for loader in loaders:
        path = getattr(loader, "path", None)
        # A path registry keeps its elements in `path`; a path of an unbound 1.4 option is a tuple.
        elements.extend(getattr(path, "path", path) or ())
    return elements


def _selects_rows_of_one_table(element: Any) -> bool:
    """Whether ``element`` is a SELECT of rows of one table and nothing else: columns of the table, WHERE
    criteria and ORDER BY on it, and no joins, grouping, DISTINCT, LIMIT, prefixes or nested SELECTs.

    Filtering the rows of such a SELECT outside of it keeps the rows that filtering inside it would.
    """
    if not isinstance(element, Select) or not element._raw_columns:
        return False
    if (
        element._setup_joins
        or getattr(element, "_legacy_setup_joins", ())
        or getattr(element, "_memoized_select_entities", ())
        or getattr(element, "_independent_ctes", ())
        or element._group_by_clauses
        or element._having_criteria
        or element._distinct
        or element._distinct_on
        or element._limit_clause is not None
        or element._offset_clause is not None
        or element._fetch_clause is not None
        or element._prefixes
        or element._suffixes
    ):
        return False

    tables: set[Table] = set()
    for column in element._raw_columns:
        while isinstance(column, Label):
            column = column.element
        column = column._deannotate() if isinstance(column, Annotated) else column
        table = column.table if isinstance(column, Column) else column
        if not isinstance(table, Table):
            return False
        tables.add(table)
    implied = itertools.chain.from_iterable(criterion._from_objects for criterion in element._where_criteria)
    for from_obj in itertools.chain(element._from_obj, implied):
        from_obj = from_obj._deannotate() if isinstance(from_obj, Annotated) else from_obj
        if not isinstance(from_obj, Table):
            return False
        tables.add(from_obj)
    if len(tables) != 1:
        return False
    return not any(isinstance(child, SelectBase) for child in visitors.iterate(element) if child is not element)


def _mapper_selectables(mapper: Mapper[Any]) -> list[Any]:
    """What a mapper selects from: its table, or its with_polymorphic() selectable."""
    return [mapper.local_table, mapper.persist_selectable, mapper.selectable, mapper._with_polymorphic_selectable]


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
    table = table._deannotate() if isinstance(table, Annotated) else table
    return any(table is mapped for mapped in mapper.tables)


def _ungrouped(from_obj: Any) -> Any:
    """The FROM element inside parentheses (a JOIN nested on the right of another JOIN is grouped)."""
    while isinstance(from_obj, FromGrouping):
        from_obj = from_obj.element
    return from_obj


def _relationship_join_parts(stmt: Select[Any]) -> list[QueryableAttribute[Any]]:
    """The relationship attributes of a statement's joins (SQLAlchemy 1.4 keeps Query joins apart)."""
    joins = itertools.chain(stmt._setup_joins, getattr(stmt, "_legacy_setup_joins", ()))
    return [part for join in joins for part in join[:3] if isinstance(part, QueryableAttribute)]


def _can_rebuild(insp: AliasedInsp[Any]) -> bool:
    """Whether an ``aliased()`` entity can be recreated over another selectable (not ``with_polymorphic()``)."""
    return insp._base_alias() is insp and len(insp.with_polymorphic_mappers) <= 1


def _alias_over(insp: AliasedInsp[Any], subquery: Subquery) -> AliasedInsp[Any]:
    """An ``aliased()`` entity like ``insp``, over ``subquery``."""
    entity = AliasedClass(
        insp.mapper,
        alias=subquery,
        name=insp.name,
        adapt_on_names=insp._adapt_on_names,
        use_mapper_path=insp._use_mapper_path,
    )
    return cast("AliasedInsp[Any]", inspect(entity))


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


def _copy_subquery(subquery: Subquery, element: Any) -> Subquery:
    """A copy of ``subquery`` (same name, so the same SQL) around a different statement."""
    copy = subquery._clone()
    copy.element = element
    copy._reset_column_collection()
    return copy


def _replace_in_statement(
    stmt: Select[Any],
    copies: dict[int, Subquery],
    aliases: dict[int, tuple[AliasedInsp[Any], AliasedInsp[Any]]],
) -> Select[Any]:
    """A copy of ``stmt`` in which subqueries are replaced by their ``copies``, and ``aliases`` entities
    (keyed by the id of the old entity) by the new entities over those copies."""
    replacements: dict[int, tuple[Any, Any]] = {}
    for old, new in aliases.values():
        replacements[id(old)] = (old, new)
        replacements[id(old.entity)] = (old.entity, new.entity)

    def swap(value: Any) -> Any:
        pair = replacements.get(id(value))
        return pair[1] if pair is not None and pair[0] is value else value

    def plain(element: Any, **kw: Any) -> Any:
        copy = copies.get(id(element))
        if copy is not None:
            return copy
        if isinstance(element, ColumnClause) and element.table is not None:
            copy = copies.get(id(element.table))
            if copy is not None:
                column = copy.c[element.key]
                return column._annotate(element._annotations) if element._annotations else column
        return None

    def replace(element: Any, **kw: Any) -> Any:
        if isinstance(element, Annotated):
            annotations = element._annotations
            if any(swap(value) is not value for value in annotations.values()):
                # An element of a replaced aliased() entity: the same element, of the new entity.
                rebuilt = visitors.replacement_traverse(element._deannotate(), {}, plain)
                return rebuilt._annotate({key: swap(value) for key, value in annotations.items()})
            if isinstance(annotations.get("parententity"), AliasedInsp):
                # The ORM tracks other aliased() entities by identity: keep these objects as they are.
                return element
        return plain(element)

    # Executable options (loader options, with_loader_criteria) are not traversable clause elements.
    stop_on = [option for element in visitors.iterate(stmt) for option in getattr(element, "_with_options", ())]
    rewritten = cast(Select[Any], visitors.replacement_traverse(stmt, {"stop_on": stop_on}, replace))
    if not aliases:
        return rewritten

    def rebuild(part: Any) -> Any:
        if not isinstance(part, QueryableAttribute):
            return part
        parent, of_type = part.parent, part._of_type
        if swap(parent) is parent and (of_type is None or swap(of_type) is of_type):
            return part
        attribute = getattr(swap(parent).entity, part.key)
        if of_type is not None:
            attribute = attribute.of_type(swap(of_type).entity)
        if part._extra_criteria:
            attribute = attribute.and_(
                *(visitors.replacement_traverse(c, {"stop_on": stop_on}, replace) for c in part._extra_criteria)
            )
        return attribute

    # The new statement is this function's own copy: setting its attributes modifies nothing else.
    rewritten._setup_joins = tuple((rebuild(j[0]), rebuild(j[1]), *j[2:]) for j in rewritten._setup_joins)
    legacy_joins = getattr(rewritten, "_legacy_setup_joins", None)
    if legacy_joins:
        rewritten._legacy_setup_joins = tuple(  # type: ignore[attr-defined]
            (rebuild(j[0]), rebuild(j[1]), *j[2:]) for j in legacy_joins
        )
    subject = rewritten._propagate_attrs.get("plugin_subject")
    if subject is not None and swap(subject) is not subject:
        rewritten._propagate_attrs = immutabledict(rewritten._propagate_attrs).union({"plugin_subject": swap(subject)})
    return rewritten
