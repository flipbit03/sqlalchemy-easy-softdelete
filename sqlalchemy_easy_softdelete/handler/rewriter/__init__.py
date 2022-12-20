"""Main query rewriter logic."""

from typing import TypeVar, Union

from sqlalchemy import Table
from sqlalchemy.orm import FromStatement
from sqlalchemy.orm.util import _ORMJoin
from sqlalchemy.sql import Alias, CompoundSelect, Join, Select, Subquery, TableClause
from sqlalchemy.sql.elements import TextClause

Statement = TypeVar('Statement', bound=Union[Select, FromStatement])


class SoftDeleteQueryRewriter:
    """Rewrites SQL statements based on configuration."""

    def __init__(self, deleted_field_name: str, disable_soft_delete_option_name: str):
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
        self.deleted_field_name = deleted_field_name
        self.disable_soft_delete_option_name = disable_soft_delete_option_name

    def rewrite_statement(self, stmt: Statement) -> Statement:
        """Rewrite a single SQL-like Statement."""
        if isinstance(stmt, Select):
            return self.rewrite_select(stmt)

        if isinstance(stmt, FromStatement):
            # Explicitly protect against INSERT with RETURNING
            if not isinstance(stmt.element, Select):
                return stmt
            stmt.element = self.rewrite_select(stmt.element)
            return stmt

        raise NotImplementedError(f"Unsupported statement type \"{(type(stmt))}\"!")

    def rewrite_select(self, stmt: Select) -> Select:
        """Rewrite a Select Statement."""
        # if the user tagged this query with an execution_option to disable soft-delete filtering
        # simply return back the same stmt
        if stmt.get_execution_options().get(self.disable_soft_delete_option_name):
            return stmt

        for from_obj in stmt.get_final_froms():
            stmt = self.analyze_from(stmt, from_obj)

        return stmt

    def rewrite_compound_select(self, stmt: CompoundSelect) -> CompoundSelect:
        """Rewrite a Compound Select Statement."""
        # This needs to be done by array slice referencing instead of
        # a direct reassignment because the reassignment would not substitute the
        # value which is inside the CompoundSelect "by reference"
        for i in range(len(stmt.selects)):
            stmt.selects[i] = self.rewrite_select(stmt.selects[i])
        return stmt

    def rewrite_element(self, subquery: Subquery) -> Subquery:
        """Rewrite an object with a `.element` attribute and patch the query inside it."""
        if isinstance(subquery.element, CompoundSelect):
            subquery.element = self.rewrite_compound_select(subquery.element)
            return subquery

        if isinstance(subquery.element, Select):
            subquery.element = self.rewrite_select(subquery.element)
            return subquery

        raise NotImplementedError(f"Unsupported object \"{(type(subquery.element))}\" in subquery.element")

    def rewrite_from_orm_join(self, stmt: Select, join_obj: Union[_ORMJoin, Join]) -> Select:
        # Recursive cases (multiple joins)
        if isinstance(join_obj.left, _ORMJoin) or isinstance(join_obj.left, Join):
            stmt = self.rewrite_from_orm_join(stmt, join_obj.left)

        if isinstance(join_obj.right, _ORMJoin) or isinstance(join_obj.right, Join):
            stmt = self.rewrite_from_orm_join(stmt, join_obj.right)

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
                f"Unsupported object \"{(type(from_obj.element))}\" inside Alias in " f"statement.froms"
            )

        raise NotImplementedError(f"Unsupported object \"{(type(from_obj))}\" in statement.froms")

    def rewrite_from_table(self, stmt: Select, table: Table) -> Select:
        """(possibly) Rewrite a Select based on whether the Table contains the soft-delete field or not."""
        column_obj = table.columns.get(self.deleted_field_name)

        # Caveat: The automatic "bool(column_obj)" conversion actually returns
        # a truthy value of False (?), so we have to explicitly compare against None
        if column_obj is not None:
            return stmt.filter(column_obj.is_(None))

        # Soft-delete argument was not found, return unchanged statement
        return stmt
