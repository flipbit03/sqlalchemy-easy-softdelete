"""What the rewriter's configuration decides: which tables and classes are soft-deletable, their soft-delete columns
and conditions, which statements disable filtering, and the loader criteria function of soft-deletable classes."""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Any, cast

import sqlalchemy
from sqlalchemy import Column, Table, and_, exists, inspect, or_
from sqlalchemy.orm import Mapper, RelationshipProperty
from sqlalchemy.orm.util import AliasedInsp
from sqlalchemy.sql import Alias, CompoundSelect, Select, visitors
from sqlalchemy.sql.elements import ColumnClause
from sqlalchemy.sql.selectable import TextualSelect

from sqlalchemy_easy_softdelete.handler.rewriter.clauses import (
    add_selects,
    aliased_table,
    column_property_selects,
    inner_joined_leaves,
    join_leaves,
    plain,
    selects_from_mapper,
    subquery_in,
)
from sqlalchemy_easy_softdelete.hook import IgnoredTable

PICKLE_MODULE = "sqlalchemy_easy_softdelete.handler.rewriter"
"""The module pickled instances find the criteria functions in (see ``function_named``): it re-exports them."""

_CRITERIA_TEMPLATE = (lambda cls: cls._soft_delete_field_.is_(None)).__code__
_CRITERIA_PREFIX = "_is_not_soft_deleted_"
_criteria_functions: dict[str, Callable[[Any], Any]] = {}


def is_null_criteria(field_name: str) -> Callable[[Any], Any]:
    """``lambda cls: cls.<field_name>.is_(None)``, for ``with_loader_criteria``.

    SQLAlchemy calls the lambda with each entity and each ``aliased()`` form of it, so the filter is
    built against the alias itself. A plain expression would have to be adapted to the alias, which
    SQLAlchemy does not do for an explicit ``join(alias, onclause)`` and renders invalid SQL.

    The field name cannot be a closure variable: SQLAlchemy replaces those with trackers while it
    analyzes the lambda, and caches the analyzed lambda by its code object, so a default argument
    would leak one configuration's field name into another's. The name is written into the code
    object instead, which gives each field name its own code, and so its own cache entries. Each field
    name has a single function, named after it, which pickle can find again (see ``function_named``).
    """
    name = _CRITERIA_PREFIX + field_name.encode().hex()
    function = _criteria_functions.get(name)
    if function is None:
        code = _CRITERIA_TEMPLATE.replace(
            co_names=tuple(field_name if n == "_soft_delete_field_" else n for n in _CRITERIA_TEMPLATE.co_names)
        )
        candidate = types.FunctionType(code, {}, name)
        candidate.__qualname__ = name
        candidate.__module__ = PICKLE_MODULE
        function = _criteria_functions.setdefault(name, candidate)
    return function


def function_named(name: str) -> Callable[[Any], Any] | None:
    """The criteria function pickle stored as ``name`` (``_is_not_soft_deleted_<hex of the field name>``), or None.

    Instances keep the loader criteria of the query that loaded them, for their lazy loads, so pickling
    an instance pickles them, and pickle stores a function as its module and name.
    """
    if name.startswith(_CRITERIA_PREFIX):
        try:
            field_name = bytes.fromhex(name[len(_CRITERIA_PREFIX) :]).decode()
        except ValueError:
            return None
        return is_null_criteria(field_name)
    return None


Configuration = tuple[str, str, tuple[tuple[str, str | None], ...]]
"""What a policy is made from: see ``SoftDeletePolicy.configuration``."""


class SoftDeletePolicy:
    """The soft-delete rules of one configuration. It remembers what depends only on the mapping (the conditions
    of secondaries and of classes mapped to a SELECT), which is the same for every statement."""

    def __init__(
        self,
        deleted_field_name: str,
        disable_soft_delete_option_name: str,
        ignored_tables: list[IgnoredTable],
    ) -> None:
        self.deleted_field_name = deleted_field_name
        self.disable_soft_delete_option_name = disable_soft_delete_option_name
        self.ignored_tables = ignored_tables
        self.is_not_deleted = is_null_criteria(deleted_field_name)
        """The loader criteria function of soft-deletable classes (see ``is_null_criteria``)."""
        self._mapped_subquery_conditions: dict[type, Any] = {}
        self._secondary_conditions: dict[RelationshipProperty[Any], Any] = {}
        # The SELECTs of each mapper's column_property() expressions that disable soft-delete filtering, with
        # what is nested in them (see disabled_selects_of): they belong to the mapping, the same for every statement.
        self._disabled_column_property_selects: dict[Mapper[Any], frozenset[int]] = {}

    def configuration(self) -> Configuration:
        """What a policy of the same configuration is made from: see ``SoftDeleteCriteria.__reduce__``."""
        ignored = tuple((table.name, table.table_schema) for table in self.ignored_tables)
        return (self.deleted_field_name, self.disable_soft_delete_option_name, ignored)

    def is_disabled(self, stmt: Any) -> bool:
        """Whether the statement disables soft-delete filtering with the configured execution option."""
        return bool(stmt.get_execution_options().get(self.disable_soft_delete_option_name))

    def collect_table(self, table: Table, columns: list[Column[Any]]) -> None:
        """Collect the soft-delete column of ``table``, unless it is ignored or has none."""
        if any(ignored.match_name(table) for ignored in self.ignored_tables):
            return

        # Caveat: The automatic "bool(column_obj)" conversion actually returns a truthy value of False (?),
        # so we have to explicitly compare against None
        column_obj = table.columns.get(self.deleted_field_name)
        if column_obj is not None:
            columns.append(column_obj)

    def alias_column(self, alias: Alias) -> Any:
        """The soft-delete column of an alias of a table, or None when the table is ignored or has none."""
        table = cast(Table, aliased_table(alias))
        columns: list[Column[Any]] = []
        self.collect_table(table, columns)
        return alias.corresponding_column(columns[0]) if columns else None

    def is_soft_deletable_entity(self, entity_class: type) -> bool:
        """Whether ``entity_class`` has the soft-delete column and its table is not ignored."""
        if not hasattr(entity_class, self.deleted_field_name):
            return False

        table = getattr(entity_class, "__table__", None)
        if isinstance(table, Table) and any(ignored.match_name(table) for ignored in self.ignored_tables):
            return False

        return True

    def soft_delete_condition(
        self, secondary: Any, *, outer_joined: bool = False, linking: set[int] | frozenset[int] = frozenset()
    ) -> Any:
        """The condition that leaves out the soft-deleted rows of a table, or of the soft-deletable tables of a
        subquery (on the soft-delete column it passes on, or through the primary key it passes on), or None:
        for a relationship's secondary, or a class mapped to a subquery.

        A table the subquery outer joins leaves NULLs for its soft-deleted rows, which keeps the subquery's row:
        it is left out, unless ``outer_joined``, or unless one of the subquery's columns from it is in
        ``linking`` (by id: the columns a relationship joins on). SQLAlchemy selects from its own alias of the
        subquery, where those rows' values show: a mapped class's joined eager load selects them, and a
        relationship would join on them.
        """
        if isinstance(secondary, Table):
            columns: list[Column[Any]] = []
            self.collect_table(secondary, columns)
            return columns[0].is_(None) if columns else None
        subquery = subquery_in(secondary)
        if subquery is None or not isinstance(subquery.element, Select) or self.is_disabled(subquery.element):
            return None
        conditions = []
        for from_obj in subquery.element.get_final_froms():
            inner = {id(leaf) for leaf in inner_joined_leaves(from_obj)}
            for table in join_leaves(from_obj):
                if not isinstance(table, Table):
                    continue
                if not outer_joined and id(table) not in inner and not _links_on(secondary, table, linking):
                    continue
                condition = self._table_condition(secondary, table)
                if condition is not None:
                    conditions.append(condition)
        return and_(*conditions) if conditions else None

    def _table_condition(self, secondary: Any, table: Table) -> Any:
        """The condition that leaves out the soft-deleted rows of ``table`` in the subquery ``secondary``: on the
        soft-delete column it passes on, or through the primary key it passes on; None when it passes on neither."""
        columns: list[Column[Any]] = []
        self.collect_table(table, columns)
        if not columns:
            return None
        passed_on = secondary.corresponding_column(columns[0])
        if passed_on is not None:
            return passed_on.is_(None)
        keys = [(column, secondary.corresponding_column(column)) for column in table.primary_key.columns]
        if not keys or any(proxy is None for _, proxy in keys):
            return None
        # An alias of the table, which SQLAlchemy does not adapt to its alias of the secondary. A NULL key
        # (the table is outer joined, without a row) passes, as a NULL soft-delete column would.
        rows = table.alias()
        return or_(
            and_(*(proxy.is_(None) for _, proxy in keys)),
            exists().where(
                *(rows.c[column.key] == proxy for column, proxy in keys),
                rows.c[columns[0].key].is_(None),
            ),
        )

    def secondary_condition(self, prop: RelationshipProperty[Any]) -> Any:
        """The soft-delete condition of the secondary of a relationship (see ``soft_delete_condition``), or None."""
        if prop.secondary is None:
            return None
        if prop not in self._secondary_conditions:
            # The relationship's conditions hold annotated copies of the secondary's columns.
            secondary = plain(prop.secondary)
            linking = {
                id(secondary.c[element.key])
                for clause in (prop.primaryjoin, prop.secondaryjoin)
                if clause is not None
                for element in visitors.iterate(clause)
                if isinstance(element, ColumnClause)
                and plain(element.table) is secondary
                and element.key in secondary.c
            }
            condition = self.soft_delete_condition(prop.secondary, linking=linking)
            return self._secondary_conditions.setdefault(prop, condition)
        return self._secondary_conditions[prop]

    def mapped_subquery_condition(self, entity_class: type) -> Any:
        """For a class mapped to a SELECT of soft-deletable tables that leaves out their soft-delete column, the
        condition that leaves out its soft-deleted rows (see ``soft_delete_condition``), or None.

        It is the class's eager loading criteria: SQLAlchemy joins an alias of the SELECT that it makes while
        it compiles a joined eager load, which the rewrite does not replace, and adapts the criteria to it.
        """
        if entity_class not in self._mapped_subquery_conditions:
            condition = None
            mapper: Any = inspect(entity_class, raiseerr=False)
            if (
                isinstance(mapper, Mapper)
                and not hasattr(entity_class, self.deleted_field_name)
                and subquery_in(mapper.local_table) is not None
            ):
                condition = _on_attributes(self.soft_delete_condition(mapper.local_table, outer_joined=True), mapper)
            return self._mapped_subquery_conditions.setdefault(entity_class, condition)
        return self._mapped_subquery_conditions[entity_class]

    def filtered_mappers_in(self, element: Any) -> tuple[list[Mapper[Any]], bool]:
        """The mappers that the statement inside a subquery refers to, leaving out SELECTs that disable
        soft-delete filtering (and what is nested in them), and whether none of it disables it.
        """
        found: dict[Mapper[Any], None] = {}
        fully_filtered = True
        stack = [element]
        while stack:
            child = stack.pop()
            if isinstance(child, (Select, CompoundSelect)) and self.is_disabled(child):
                fully_filtered = False
                continue
            if isinstance(child, TextualSelect):
                # Raw SQL is left as it is: its rows may include soft-deleted ones.
                fully_filtered = False
                continue
            mapper = getattr(child, "_annotations", {}).get("parentmapper")
            if isinstance(mapper, Mapper) and selects_from_mapper(child, mapper):
                found.setdefault(mapper, None)
            stack.extend(child.get_children())
        return list(found), fully_filtered

    def filters_alias(self, insp: AliasedInsp[Any]) -> bool:
        """Whether the eager loading criteria may filter an ``aliased()`` entity over a subquery: not when its
        statement includes soft-deleted rows, nor when it does not pass on the soft-delete column (SQLAlchemy
        would then filter the class's table, which it does not select from there)."""
        subquery = subquery_in(insp.selectable)
        if subquery is None:
            return True
        column = insp.mapper.columns.get(self.deleted_field_name)
        passed_on = column is not None and insp.selectable.corresponding_column(column) is not None
        return passed_on and self.filtered_mappers_in(subquery.element)[1]

    def disabled_selects_of(self, mapper: Mapper[Any]) -> frozenset[int]:
        """The ids of the SELECTs of ``mapper``'s column_property() expressions that disable soft-delete filtering,
        with what is nested in them."""
        found = self._disabled_column_property_selects.get(mapper)
        if found is None:
            selects: set[int] = set()
            for select in column_property_selects([mapper]):
                if self.is_disabled(select):
                    add_selects(select, selects)
            found = self._disabled_column_property_selects.setdefault(mapper, frozenset(selects))
        return found


def entity_class_of(path_element: Any) -> type | None:
    """The entity class of a loader option's path element: a mapper, or an ``aliased()`` entity."""
    if isinstance(path_element, Mapper):
        return path_element.class_

    # An aliased() entity in the path (`joinedload(Parent.children.of_type(alias))`)
    if isinstance(path_element, AliasedInsp):
        return path_element.mapper.class_

    return None


def _links_on(selectable: Any, table: Table, linking: set[int] | frozenset[int]) -> bool:
    """Whether one of the columns of ``selectable`` that come from ``table`` is in ``linking`` (by id)."""
    return any(
        id(column) in linking and any(table.c.contains_column(base) for base in column.base_columns)
        for column in selectable.c
    )


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
