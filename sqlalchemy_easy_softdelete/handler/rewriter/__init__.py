"""Main query rewriter logic."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import Table
from sqlalchemy.orm import FromStatement, Mapper, RelationshipProperty, with_loader_criteria
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.orm.strategy_options import Load
from sqlalchemy.orm.util import _ORMJoin
from sqlalchemy.sql import Alias, CompoundSelect, Executable, Join, Select, Subquery, TableClause
from sqlalchemy.sql.elements import TextClause

from sqlalchemy_easy_softdelete.hook import IgnoredTable

# Type alias for statements we can rewrite
Statement = Select | FromStatement | CompoundSelect | Executable


class SoftDeleteQueryRewriter:
    """Rewrites SQL statements based on configuration."""

    def __init__(
        self,
        deleted_field_name: str,
        disable_soft_delete_option_name: str,
        ignored_tables: list[IgnoredTable] | None = None,
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

        """
        """List of table names that should be ignored from soft-deletion"""
        self.ignored_tables = ignored_tables or []
        self.deleted_field_name = deleted_field_name
        self.disable_soft_delete_option_name = disable_soft_delete_option_name

    def rewrite_statement(self, stmt: Statement) -> Statement:
        """Rewrite a single SQL-like Statement."""
        if isinstance(stmt, Select):
            return self.rewrite_select(stmt)

        # Handle CompoundSelect
        if isinstance(stmt, CompoundSelect):
            return self.rewrite_compound_select(stmt)

        # Handle FromStatement which is also a Select/Executable
        if isinstance(stmt, FromStatement):
            # Explicitly protect against INSERT with RETURNING
            if not isinstance(stmt.element, Select):
                return stmt
            stmt.element = self.rewrite_select(stmt.element)
            return stmt

        raise NotImplementedError(f'Unsupported statement type "{(type(stmt))}"!')

    def rewrite_select(self, stmt: Select) -> Select:
        """Rewrite a Select Statement."""
        # if the user tagged this query with an execution_option to disable soft-delete filtering
        # simply return back the same stmt
        if stmt.get_execution_options().get(self.disable_soft_delete_option_name):
            return stmt

        # Handle eager loading (joinedload, subqueryload, etc.) by adding loader criteria
        # This ensures soft-delete filters are applied to JOINed aliases
        stmt = self._add_loader_criteria_for_eager_loads(stmt)

        for from_obj in stmt.get_final_froms():
            stmt = self.analyze_from(stmt, from_obj)

        return stmt

    def _add_loader_criteria_for_eager_loads(self, stmt: Select) -> Select:
        """Add with_loader_criteria for entities loaded via eager loading options.

        Handles both SQLAlchemy 1.4 and 2.0 structures:
        - 1.4: opt.path is a tuple of InstrumentedAttribute, entity via property.mapper.class_
        - 2.0: opt.path is SlotsEntityRegistry with Mapper objects, entity via class_
        """
        # Access _with_options - an internal attribute for ORM loader options.
        # This is part of SQLAlchemy's _traverse_internals (dp_executable_options),
        # meaning it's stable across versions. There is no public API to read options.
        if not stmt._with_options:
            return stmt

        # Collect all entity classes from the loading paths
        entities_to_filter: set[type] = set()

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
                if entity_class is None:
                    continue

                # Check if this entity has the soft-delete column
                if not hasattr(entity_class, self.deleted_field_name):
                    continue

                # Check if the table is in the ignored list
                table = getattr(entity_class, "__table__", None)
                if isinstance(table, Table):
                    if any(ignored.match_name(table) for ignored in self.ignored_tables):
                        continue

                entities_to_filter.add(entity_class)

        # Add with_loader_criteria for each entity
        for entity_class in entities_to_filter:
            deleted_attr = getattr(entity_class, self.deleted_field_name)
            stmt = stmt.options(
                with_loader_criteria(
                    entity_class,
                    deleted_attr.is_(None),
                    include_aliases=True,
                )
            )

        return stmt

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

    def rewrite_compound_select(self, stmt: CompoundSelect) -> CompoundSelect:
        """Rewrite a Compound Select Statement."""
        # This needs to be done by array slice referencing instead of
        # a direct reassignment because the reassignment would not substitute the
        # value which is inside the CompoundSelect "by reference"
        for i in range(len(stmt.selects)):
            stmt.selects[i] = self.rewrite_select(cast(Select[Any], stmt.selects[i]))
        return stmt

    def rewrite_element(self, subquery: Subquery) -> Subquery:
        """Rewrite an object with a `.element` attribute and patch the query inside it."""
        if isinstance(subquery.element, CompoundSelect):
            subquery.element = self.rewrite_compound_select(subquery.element)
            return subquery

        if isinstance(subquery.element, Select):
            subquery.element = self.rewrite_select(subquery.element)
            return subquery

        raise NotImplementedError(f'Unsupported object "{(type(subquery.element))}" in subquery.element')

    def rewrite_from_orm_join(self, stmt: Select, join_obj: _ORMJoin | Join) -> Select:
        """Handle multiple, and potentially recursive joins."""

        # Recursive cases (multiple joins)
        if isinstance(join_obj.left, _ORMJoin) or isinstance(join_obj.left, Join):
            stmt = self.rewrite_from_orm_join(stmt, join_obj.left)

        if isinstance(join_obj.right, _ORMJoin) or isinstance(join_obj.right, Join):
            stmt = self.rewrite_from_orm_join(stmt, join_obj.right)

        # Handle Subqueries (e.g., from subquery joins)
        if isinstance(join_obj.left, Subquery):
            self.rewrite_element(join_obj.left)

        if isinstance(join_obj.right, Subquery):
            self.rewrite_element(join_obj.right)

        # Normal cases - Tables
        if isinstance(join_obj.left, Table):
            stmt = self.rewrite_from_table(stmt, join_obj.left)

        if isinstance(join_obj.right, Table):
            stmt = self.rewrite_from_table(stmt, join_obj.right)

        return stmt

    def analyze_from(self, stmt: Select, from_obj):
        """Analyze the FROMS of a Select to determine possible soft-delete rewritable tables."""
        if isinstance(from_obj, Table):
            return self.rewrite_from_table(stmt, from_obj)

        if isinstance(from_obj, _ORMJoin) or isinstance(from_obj, Join):
            # _ORMJOIN/Join contains information about two things: 'left' and 'right'. Check both.
            return self.rewrite_from_orm_join(stmt, from_obj)

        if isinstance(from_obj, Subquery):
            self.rewrite_element(from_obj)
            return stmt

        if isinstance(from_obj, TableClause) or isinstance(from_obj, TextClause):
            # TableClause/TextClause objects are raw text SQL identifiers and as such, we cannot
            # introspect or do anything about this statement.
            return stmt

        if isinstance(from_obj, Alias):
            if isinstance(from_obj.element, Subquery):
                self.rewrite_element(from_obj.element)
                return stmt

            raise NotImplementedError(
                f'Unsupported object "{(type(from_obj.element))}" inside Alias in statement.froms'
            )

        raise NotImplementedError(f'Unsupported object "{(type(from_obj))}" in statement.froms')

    def rewrite_from_table(self, stmt: Select, table: Table) -> Select:
        """
        (possibly) Rewrite a Select based on whether the Table contains the soft-delete field or not.

        Ignore tables named like the ignore_tabl

        """
        # Early return if the table is ignored
        if any(ignored.match_name(table) for ignored in self.ignored_tables):
            return stmt

        # Try to retrieve the column object
        column_obj = table.columns.get(self.deleted_field_name)

        # If the column object is not found, return unchanged statement
        # Caveat: The automatic "bool(column_obj)" conversion actually returns a truthy value of False (?),
        # so we have to explicitly compare against None
        if column_obj is None:
            return stmt

        # Column found. Rewrite the statement with a filter condition in the soft-delete column
        return stmt.filter(column_obj.is_(None))
