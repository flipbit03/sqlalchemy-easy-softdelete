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
from sqlalchemy.orm.attributes import InstrumentedAttribute, QueryableAttribute
from sqlalchemy.orm.strategy_options import Load
from sqlalchemy.orm.util import AliasedClass, AliasedInsp
from sqlalchemy.sql import Alias, CompoundSelect, Executable, Join, Select, Subquery, TableClause, visitors
from sqlalchemy.sql.annotation import Annotated
from sqlalchemy.sql.elements import ColumnClause, TextClause
from sqlalchemy.sql.lambdas import LambdaElement
from sqlalchemy.sql.selectable import FromGrouping, SelectBase, SelectStatementGrouping, Values

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
    """Soft-delete columns of the tables in the FROM clause, filtered with ``IS NULL``."""

    criteria_entities: tuple[tuple[type, bool], ...]
    """Mapped classes filtered through ``with_loader_criteria`` options on this statement (eager loading),
    each with whether the filter also applies to the class's ``aliased()`` forms."""

    outer_criteria_entities: tuple[tuple[type, bool], ...]
    """Mapped classes filtered through ``with_loader_criteria`` options on the outermost statement, for
    subqueries that must stay as they are. SQLAlchemy only applies loader criteria of the statement it
    executes, and from there they reach the SELECTs nested in it."""

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
    statement, ``with_polymorphic()``), the subquery is "fixed": it stays as it is, and its tables are
    filtered at compile time instead. So is a subquery the statement does not refer to at all.
    """

    def __init__(self, stmt: Select[Any]) -> None:
        self.held: set[int] = set()
        self.aliases: dict[int, list[AliasedInsp[Any]]] = {}
        self.fixed: set[int] = set()

        for element in visitors.iterate(stmt):
            if isinstance(element, Annotated):
                self._note(element._annotations.get("parententity"))
            else:
                self.held.add(id(element))
            if not isinstance(element, Select):
                continue
            # Relationship joins (`join(Parent.children.of_type(alias))`) hold attributes, which the
            # traversal does not reach. Only those of the statement itself are rebuilt.
            for part in _relationship_join_parts(element):
                self._note(part.parent, fixed=element is not stmt)
                self._note(part._of_type, fixed=element is not stmt)
            for option in element._with_options:
                if isinstance(option, Load) and option.path is not None:
                    for path_element in option.path:  # type: ignore[attr-defined]
                        self._note(getattr(path_element, "parent", path_element), fixed=True)

    def _note(self, entity: Any, fixed: bool = False) -> None:
        insp = inspect(entity, raiseerr=False) if entity is not None else None
        if not isinstance(insp, AliasedInsp):
            return
        key = id(insp.selectable)
        insps = self.aliases.setdefault(key, [])
        if not any(known is insp for known in insps):
            insps.append(insp)
        if fixed or not _can_rebuild(insp):
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
        outer_criteria: _Criteria = {}
        rewritten = self._rewrite_select(stmt, outer_criteria)
        return self._with_criteria(rewritten, outer_criteria)

    def rewrite_compound_select(self, stmt: CompoundSelect) -> CompoundSelect:
        """Rewrite a Compound Select Statement."""
        outer_criteria: _Criteria = {}
        rewritten = self._rewrite_compound_select(stmt, outer_criteria)
        return self._with_criteria(rewritten, outer_criteria)

    def _rewrite_select(self, stmt: Select[Any], outer_criteria: _Criteria) -> Select[Any]:
        """Rewrite a Select, collecting into ``outer_criteria`` the loader criteria that the outermost
        statement must carry."""
        # if the user tagged this query with an execution_option to disable soft-delete filtering
        # simply return back the same stmt
        if self._is_disabled(stmt):
            return stmt

        plan, analysis = self._plan_for(stmt)
        return self._apply_plan(stmt, plan, analysis, outer_criteria)

    def _rewrite_compound_select(self, stmt: CompoundSelect, outer_criteria: _Criteria) -> CompoundSelect:
        if self._is_disabled(stmt):
            return stmt
        selects: list[SelectBase] = [self._rewrite_compound_member(select, outer_criteria) for select in stmt.selects]
        if all(new is old for new, old in zip(selects, stmt.selects)):
            return stmt
        rewritten = stmt._generate()
        rewritten.selects = selects
        return rewritten

    def _rewrite_compound_member(self, member: Any, outer_criteria: _Criteria) -> Any:
        """Rewrite one SELECT of a union: a SELECT or a nested union, possibly in parentheses (a member
        with ORDER BY or LIMIT, or a nested union, is held in a grouping)."""
        if isinstance(member, SelectStatementGrouping):
            element = self._rewrite_compound_member(member.element, outer_criteria)
            return member if element is member.element else SelectStatementGrouping(element)
        if isinstance(member, CompoundSelect):
            return self._rewrite_compound_select(member, outer_criteria)
        return self._rewrite_select(cast(Select[Any], member), outer_criteria)

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
        for from_obj in _final_froms_without_loader_options(stmt):
            self._collect_from(from_obj, columns, subqueries)

        # Eager loading joins aliases of the loaded classes: their filter applies to aliased() forms.
        entities: _Criteria = dict.fromkeys(self._eager_loaded_entities(stmt), True)
        outer_entities: _Criteria = {}
        held: list[Subquery] = []
        aliased: dict[int, tuple[Subquery, list[AliasedInsp[Any]]]] = {}
        reusable = True
        references = _StatementReferences(stmt) if subqueries else None
        for subquery in dict.fromkeys(subqueries):
            assert references is not None
            selectable: Subquery = subquery
            insps = references.aliases.get(id(subquery))
            if insps is None and isinstance(subquery, Annotated):
                # An explicit join to an aliased() entity puts the entity's annotated subquery in the
                # FROM clause, rather than the subquery itself.
                insp = subquery._annotations.get("parententity")
                if isinstance(insp, AliasedInsp):
                    selectable = cast(Subquery, insp.selectable)
                    insps = references.aliases.get(id(selectable), [insp])

            if insps is None and id(subquery) in references.held:
                held.append(subquery)
                continue

            reusable = False
            if insps is not None and id(selectable) not in references.fixed and all(map(_can_rebuild, insps)):
                aliased.setdefault(id(selectable), (selectable, insps))
                continue

            # The subquery must stay as it is: filter its tables at compile time.
            self._collect_compile_time_criteria(selectable.element, insps or [], outer_entities)

        if held and {id(subquery) for subquery in _held_from_subqueries(stmt)} != {id(s) for s in held}:
            # A later statement of this shape would not find its subqueries without this analysis.
            reusable = False

        plan = _SelectPlan(
            tuple(columns),
            tuple(entities.items()),
            tuple(outer_entities.items()),
            rewrites_subqueries=bool(held),
            reusable=reusable,
        )
        return plan, _FromAnalysis(held, list(aliased.values()))

    def _collect_compile_time_criteria(self, element: Any, insps: list[AliasedInsp[Any]], criteria: _Criteria) -> None:
        """Criteria that filter, at compile time, the tables of a subquery that must stay as it is."""
        mappers, fully_filtered = self._filtered_mappers_in(element)
        for mapper in mappers:
            if self._is_soft_deletable_entity(mapper.class_):
                # The entities the statement inside refers to are filtered inside it.
                criteria.setdefault(mapper.class_, False)
        inner_classes = {mapper.class_ for mapper in mappers}
        for insp in insps:
            entity_class = insp.mapper.class_
            if fully_filtered and entity_class not in inner_classes and self._is_soft_deletable_entity(entity_class):
                # The statement inside does not refer to the entity (a Core SELECT of its table):
                # filter the aliased entity itself.
                criteria[entity_class] = True

    def _apply_plan(
        self, stmt: Select[Any], plan: _SelectPlan, analysis: _FromAnalysis | None, outer_criteria: _Criteria
    ) -> Select[Any]:
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
            element = self._rewrite_subquery_element(subquery.element, outer_criteria)
            if element is not subquery.element:
                copies[id(subquery)] = _copy_subquery(subquery, element)
        for subquery, insps in analysis.aliased:
            element = self._rewrite_subquery_element(subquery.element, outer_criteria)
            if element is subquery.element:
                continue
            copy = _copy_subquery(subquery, element)
            copies[id(subquery)] = copy
            for insp in insps:
                aliases[id(insp)] = (insp, _alias_over(insp, copy))
        if copies:
            stmt = _replace_in_statement(stmt, copies, aliases)

        stmt = self._with_criteria(stmt, dict(plan.criteria_entities))

        for column in plan.filter_columns:
            stmt = stmt.filter(column.is_(None))

        for entity, include_aliases in plan.outer_criteria_entities:
            outer_criteria[entity] = outer_criteria.get(entity, False) or include_aliases
        return stmt

    def _collect_from(self, from_obj: Any, columns: list[Column[Any]], subqueries: list[Subquery]) -> None:
        """Collect the soft-delete columns and subqueries of one element of a statement's FROM list."""
        if isinstance(from_obj, Table):
            self._collect_table(from_obj, columns)
            return

        if isinstance(from_obj, Join):
            # Join (and the ORM's _ORMJoin) holds two things: 'left' and 'right'. Check both.
            self._collect_join(from_obj, columns, subqueries)
            return

        if isinstance(from_obj, Subquery):
            subqueries.append(from_obj)
            return

        if isinstance(from_obj, FromGrouping):
            # A parenthesized FROM element, such as a JOIN nested on the right of another JOIN.
            self._collect_from(from_obj.element, columns, subqueries)
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

    def _collect_join(self, join: Join, columns: list[Column[Any]], subqueries: list[Subquery]) -> None:
        """Handle multiple, and potentially recursive joins: nested joins, then subqueries, then tables."""
        sides = (_ungrouped(join.left), _ungrouped(join.right))
        for side in sides:
            if isinstance(side, Join):
                self._collect_join(side, columns, subqueries)
        for side in sides:
            if isinstance(side, Subquery):
                subqueries.append(side)
        for side in sides:
            if isinstance(side, Table):
                self._collect_table(side, columns)

    def _collect_table(self, table: Table, columns: list[Column[Any]]) -> None:
        """Collect the soft-delete column of ``table``, unless the table is ignored or has none."""
        if any(ignored.match_name(table) for ignored in self.ignored_tables):
            return

        # Caveat: The automatic "bool(column_obj)" conversion actually returns a truthy value of False (?),
        # so we have to explicitly compare against None
        column_obj = table.columns.get(self.deleted_field_name)
        if column_obj is not None:
            columns.append(column_obj)

    def _rewrite_subquery_element(self, element: Any, outer_criteria: _Criteria) -> Any:
        """Rewrite the statement inside a subquery."""
        if isinstance(element, CompoundSelect):
            return self._rewrite_compound_select(element, outer_criteria)

        if isinstance(element, Select):
            return self._rewrite_select(element, outer_criteria)

        raise NotImplementedError(f'Unsupported object "{(type(element))}" in subquery.element')

    def _filtered_mappers_in(self, element: Any) -> tuple[list[Mapper[Any]], bool]:
        """The mappers that the statement inside a subquery refers to, leaving out SELECTs that disable
        soft-delete filtering, and whether no SELECT of it disables it.
        """
        if isinstance(element, CompoundSelect):
            mappers: list[Mapper[Any]] = []
            fully_filtered = True
            for select in element.selects:
                select_mappers, select_filtered = self._filtered_mappers_in(select)
                mappers.extend(select_mappers)
                fully_filtered = fully_filtered and select_filtered
            return mappers, fully_filtered

        if isinstance(element, Select) and element.get_execution_options().get(self.disable_soft_delete_option_name):
            return [], False

        found: dict[Mapper[Any], None] = {}
        for child in visitors.iterate(element):
            mapper = child._annotations.get("parentmapper")
            if isinstance(mapper, Mapper):
                found.setdefault(mapper, None)
        return list(found), True

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
            for path_element in path:  # type: ignore[attr-defined]
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
        - 1.4: InstrumentedAttribute -> property.mapper.class_
        - 2.0: Mapper -> class_
        """
        # SQLAlchemy 2.0: Mapper objects have class_ directly
        if isinstance(path_element, Mapper):
            return path_element.class_

        # SQLAlchemy 1.4: InstrumentedAttribute -> property.mapper.class_
        if isinstance(path_element, InstrumentedAttribute):
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
    """
    if not stmt._with_options:
        return stmt.get_final_froms()
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
        rewritten._propagate_attrs = rewritten._propagate_attrs.union({"plugin_subject": swap(subject)})
    return rewritten
