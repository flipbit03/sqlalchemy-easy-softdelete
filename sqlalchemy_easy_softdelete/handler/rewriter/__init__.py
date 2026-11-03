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
from sqlalchemy.orm.util import AliasedInsp
from sqlalchemy.sql import Alias, CompoundSelect, Executable, Join, Select, Subquery, TableClause, visitors
from sqlalchemy.sql.annotation import Annotated
from sqlalchemy.sql.elements import ColumnClause, TextClause
from sqlalchemy.sql.lambdas import LambdaElement
from sqlalchemy.sql.selectable import SelectBase, Values

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
    """SELECTs whose FROM clause had to be analyzed."""

    size: int
    """Statement shapes currently remembered."""

    max_size: int
    """The ``rewrite_cache_size`` the rewriter was created with."""


@dataclass(frozen=True)
class _SelectPlan:
    """What the soft-delete rewrite of one SELECT adds to it."""

    filter_columns: tuple[Column[Any], ...]
    """Soft-delete columns of the tables in the FROM clause, filtered with ``IS NULL``."""

    criteria_entities: tuple[tuple[type, bool], ...]
    """Mapped classes filtered through ``with_loader_criteria``, which SQLAlchemy applies at compile time,
    each with whether the filter also applies to the class's ``aliased()`` forms."""

    rewrites_subqueries: bool
    """Whether subqueries the statement holds in its FROM clause are replaced by rewritten copies."""

    reusable: bool
    """Whether the plan holds for every statement with the same cache key.

    It does not when a subquery is reached through an ``aliased()`` entity: the plan then depends on
    execution options of the statement inside it, which cache keys leave out. Nor when the subqueries
    a statement holds cannot be found again without analyzing its FROM clause.
    """


class _PlanCache:
    """Plans of reusable rewrites by statement cache key, least recently used first out."""

    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        self.hits = 0
        self.misses = 0
        self._plans: OrderedDict[Any, _SelectPlan] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> _SelectPlan | None:
        with self._lock:
            plan = self._plans.get(key)
            if plan is None:
                self.misses += 1
                return None
            self._plans.move_to_end(key)
            self.hits += 1
            return plan

    def put(self, key: Any, plan: _SelectPlan) -> None:
        with self._lock:
            self._plans[key] = plan
            self._plans.move_to_end(key)
            while len(self._plans) > self.max_size:
                self._plans.popitem(last=False)

    def __len__(self) -> int:
        return len(self._plans)


class _StatementReferences:
    """How a statement refers to the subqueries in its FROM clause.

    A subquery the statement holds directly (``join(sq)``, ``select(sq.c.x)``, ``select_from(sq)``)
    can be swapped for a rewritten copy. A subquery the ORM reaches through an ``aliased()`` entity
    cannot: the entity keeps pointing at the original object, which the ORM renders. A subquery the
    statement does not refer to at all was built by the ORM while compiling it.
    """

    def __init__(self, stmt: Select[Any]) -> None:
        self.held: set[int] = set()
        self.aliased_mappers: dict[int, list[Mapper[Any]]] = {}

        for element in visitors.iterate(stmt):
            if isinstance(element, Annotated):
                self._note(element._annotations.get("parententity"))
            else:
                self.held.add(id(element))

        # ORM joins through relationships (`join(Parent.children.of_type(alias))`) are not reached by
        # the traversal above. SQLAlchemy 1.4 keeps Query joins in `_legacy_setup_joins`.
        joins = itertools.chain(stmt._setup_joins, getattr(stmt, "_legacy_setup_joins", ()))
        for join in joins:
            for part in join[:3]:
                if isinstance(part, QueryableAttribute):
                    self._note(part.parent)
                    self._note(part._of_type)

        for option in stmt._with_options:
            if isinstance(option, Load) and option.path is not None:
                for path_element in option.path:  # type: ignore[attr-defined]
                    self._note(getattr(path_element, "parent", path_element))

    def _note(self, entity: Any) -> None:
        insp = inspect(entity, raiseerr=False) if entity is not None else None
        if isinstance(insp, AliasedInsp):
            self.aliased_mappers.setdefault(id(insp.selectable), []).append(insp.mapper)


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
            return RewriteCacheInfo(hits=0, misses=0, size=0, max_size=0)
        return RewriteCacheInfo(hits=cache.hits, misses=cache.misses, size=len(cache), max_size=cache.max_size)

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
            if not isinstance(stmt.element, Select):
                return stmt
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
        if stmt.get_execution_options().get(self.disable_soft_delete_option_name):
            return stmt

        plan, held_subqueries = self._plan_for(stmt)
        return self._apply_plan(stmt, plan, held_subqueries)

    def rewrite_compound_select(self, stmt: CompoundSelect) -> CompoundSelect:
        """Rewrite a Compound Select Statement."""
        selects: list[SelectBase] = [self.rewrite_select(cast(Select[Any], select)) for select in stmt.selects]
        if all(new is old for new, old in zip(selects, stmt.selects)):
            return stmt
        rewritten = stmt._generate()
        rewritten.selects = selects
        return rewritten

    def _plan_for(self, stmt: Select[Any]) -> tuple[_SelectPlan, list[Subquery] | None]:
        """The plan for ``stmt``, a remembered one for its shape or a new one, and, for a new one, the
        subqueries held in its FROM clause.
        """
        cache = self._plan_cache
        cache_key = stmt._generate_cache_key() if cache is not None else None
        if cache is not None and cache_key is not None:
            plan = cache.get(cache_key.key)
            if plan is not None:
                return plan, None

        plan, held_subqueries = self._plan_select(stmt)
        if cache is not None and cache_key is not None and plan.reusable:
            cache.put(cache_key.key, plan)
        return plan, held_subqueries

    def _plan_select(self, stmt: Select[Any]) -> tuple[_SelectPlan, list[Subquery]]:
        """Work out what the soft-delete rewrite adds to ``stmt``, and the subqueries it holds in its
        FROM clause.
        """
        columns: list[Column[Any]] = []
        subqueries: list[Subquery] = []
        for from_obj in _final_froms_without_loader_options(stmt):
            self._collect_from(from_obj, columns, subqueries)

        # Eager loading joins aliases of the loaded classes: their filter applies to aliased() forms.
        entities = dict.fromkeys(self._eager_loaded_entities(stmt), True)
        held: list[Subquery] = []
        reusable = True
        references = _StatementReferences(stmt) if subqueries else None
        for subquery in dict.fromkeys(subqueries):
            assert references is not None
            aliased_mappers = references.aliased_mappers.get(id(subquery))
            if aliased_mappers is None and isinstance(subquery, Annotated):
                # An explicit join to an aliased() entity puts the entity's annotated subquery in the
                # FROM clause, rather than the subquery itself.
                insp = subquery._annotations.get("parententity")
                if isinstance(insp, AliasedInsp):
                    aliased_mappers = [insp.mapper]

            if aliased_mappers is None and id(subquery) in references.held:
                held.append(subquery)
                continue

            # The ORM reaches this subquery through an aliased() entity, which keeps pointing at this
            # object (or the statement does not hold it at all): it must stay as it is, so its tables
            # are filtered at compile time instead.
            reusable = False
            mappers, fully_filtered = self._filtered_mappers_in(subquery.element)
            for mapper in mappers:
                if self._is_soft_deletable_entity(mapper.class_):
                    # The entities the statement inside refers to are filtered inside it.
                    entities.setdefault(mapper.class_, False)
            inner_classes = {mapper.class_ for mapper in mappers}
            for mapper in aliased_mappers or ():
                if fully_filtered and mapper.class_ not in inner_classes:
                    # The statement inside does not refer to the entity (a Core SELECT of its table):
                    # filter the aliased entity itself.
                    if self._is_soft_deletable_entity(mapper.class_):
                        entities[mapper.class_] = True

        if held and {id(subquery) for subquery in _held_from_subqueries(stmt)} != {id(s) for s in held}:
            # A later statement of this shape would not find its subqueries without this analysis.
            reusable = False

        plan = _SelectPlan(tuple(columns), tuple(entities.items()), rewrites_subqueries=bool(held), reusable=reusable)
        return plan, held

    def _apply_plan(self, stmt: Select[Any], plan: _SelectPlan, held_subqueries: list[Subquery] | None) -> Select[Any]:
        """Return a copy of ``stmt`` with ``plan`` applied.

        ``held_subqueries`` are the subqueries the statement holds in its FROM clause, when its FROM
        clause was just analyzed; otherwise they are found again.
        """
        if plan.rewrites_subqueries:
            if held_subqueries is None:
                held_subqueries = _held_from_subqueries(stmt)
            rewrites = []
            for subquery in held_subqueries:
                element = self._rewrite_subquery_element(subquery.element)
                if element is not subquery.element:
                    rewrites.append((subquery, _copy_subquery(subquery, element)))
            if rewrites:
                stmt = _replace_subqueries(stmt, tuple(rewrites))

        if plan.criteria_entities:
            stmt = stmt.options(
                *(
                    with_loader_criteria(entity, self._is_not_deleted, include_aliases=include_aliases)
                    for entity, include_aliases in plan.criteria_entities
                )
            )

        for column in plan.filter_columns:
            stmt = stmt.filter(column.is_(None))

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
        sides = (join.left, join.right)
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

    def _rewrite_subquery_element(self, element: Any) -> Any:
        """Rewrite the statement inside a subquery."""
        if isinstance(element, CompoundSelect):
            return self.rewrite_compound_select(element)

        if isinstance(element, Select):
            return self.rewrite_select(element)

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
        """Soft-deletable entities loaded through eager loading options (joinedload, subqueryload, etc.).

        These are filtered with ``with_loader_criteria``, so the soft-delete filter also reaches the
        JOINed aliases and the separate queries that eager loading creates.

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


class _LambdaRewriteToken:
    """Marks, in a ``lambda_stmt`` cache key, the statements one rewriter has rewritten."""

    __slots__ = ("field_name",)

    def __init__(self, field_name: str) -> None:
        self.field_name = field_name

    def __repr__(self) -> str:
        return f"<soft-delete rewrite of {self.field_name!r} at {id(self):#x}>"


_CRITERIA_TEMPLATE = (lambda cls: cls._soft_delete_field_.is_(None)).__code__


def _is_null_criteria(field_name: str) -> Callable[[Any], Any]:
    """``lambda cls: cls.<field_name>.is_(None)``, for ``with_loader_criteria``.

    SQLAlchemy calls the lambda with each entity and each ``aliased()`` form of it, so the filter is
    built against the alias itself. A plain expression would have to be adapted to the alias, which
    SQLAlchemy does not do for an explicit ``join(alias, onclause)`` and renders invalid SQL.

    The field name cannot be a closure variable: SQLAlchemy replaces those with trackers while it
    analyzes the lambda, and caches the analyzed lambda by its code object, so a default argument
    would leak one configuration's field name into another's. The name is written into the code
    object instead, which gives each field name its own code, and so its own cache entries.
    """
    code = _CRITERIA_TEMPLATE.replace(
        co_names=tuple(field_name if name == "_soft_delete_field_" else name for name in _CRITERIA_TEMPLATE.co_names)
    )
    return types.FunctionType(code, {}, "is_not_soft_deleted")


def _copy_subquery(subquery: Subquery, element: Any) -> Subquery:
    """A copy of ``subquery`` (same name, so the same SQL) around a different statement."""
    copy = subquery._clone()
    copy.element = element
    copy._reset_column_collection()
    return copy


def _replace_subqueries(stmt: Select[Any], rewrites: tuple[tuple[Subquery, Subquery], ...]) -> Select[Any]:
    """A copy of ``stmt`` in which each subquery of ``rewrites``, and its columns, is replaced by its copy."""
    by_id = {id(original): copy for original, copy in rewrites}

    def replace(element: Any, **kw: Any) -> Any:
        if isinstance(element, Annotated) and isinstance(element._annotations.get("parententity"), AliasedInsp):
            # The ORM tracks aliased() entities by identity: keep these objects as they are.
            return element
        copy = by_id.get(id(element))
        if copy is not None:
            return copy
        if isinstance(element, ColumnClause) and element.table is not None:
            copy = by_id.get(id(element.table))
            if copy is not None:
                column = copy.c[element.key]
                return column._annotate(element._annotations) if element._annotations else column
        return None

    # Executable options (loader options, with_loader_criteria) are not traversable clause elements.
    return cast(Select[Any], visitors.replacement_traverse(stmt, {"stop_on": list(stmt._with_options)}, replace))
