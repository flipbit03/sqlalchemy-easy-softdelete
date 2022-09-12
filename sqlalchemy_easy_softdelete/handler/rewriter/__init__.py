from sqlalchemy import Table
from sqlalchemy.orm.util import _ORMJoin
from sqlalchemy.sql import Alias, CompoundSelect, Join, Select, Subquery, TableClause
from sqlalchemy.sql.elements import TextClause


class SoftDeleteQueryRewriter:
    def __init__(self, deleted_field_name: str, disable_soft_delete_option_name: str):
        self.deleted_field_name = deleted_field_name
        self.disable_soft_delete_option_name = disable_soft_delete_option_name

    def rewrite_select(self, stmt: Select) -> Select:
        # if the user tagged this query with an execution_option to disable soft-delete filtering
        # simply return back the same stmt
        if stmt.get_execution_options().get(self.disable_soft_delete_option_name):
            return stmt

        for from_obj in stmt.get_final_froms():
            stmt = self.analyze_from(stmt, from_obj)

        return stmt

    def rewrite_compound_select(self, stmt: CompoundSelect) -> CompoundSelect:
        # This needs to be done by array slice referencing instead of
        # a direct reassignment because the reassignment would not substitute the
        # value which is inside the CompoundSelect "by reference"
        for i in range(len(stmt.selects)):
            stmt.selects[i] = self.rewrite_select(stmt.selects[i])
        return stmt

    def rewrite_subquery(self, subquery: Subquery) -> Subquery:
        if isinstance(subquery.element, CompoundSelect):
            subquery.element = self.rewrite_compound_select(subquery.element)
            return subquery

        if isinstance(subquery.element, Select):
            subquery.element = self.rewrite_select(subquery.element)
            return subquery

        raise NotImplementedError(f"Unsupported object \"{(type(subquery.element))}\" in subquery.element")

    def analyze_from(self, stmt: Select, from_obj):
        if isinstance(from_obj, Table):
            return self.rewrite_from_table(stmt, from_obj)

        if isinstance(from_obj, _ORMJoin) or isinstance(from_obj, Join):
            # _ORMJOIN/Join contains information about two tables: 'left' and 'right'. Check both.
            left_adapted_stmt = self.rewrite_from_table(stmt, from_obj.left)
            right_adapted_stmt = self.rewrite_from_table(left_adapted_stmt, from_obj.right)
            return right_adapted_stmt

        if isinstance(from_obj, Subquery):
            self.rewrite_subquery(from_obj)
            return stmt

        if isinstance(from_obj, TableClause) or isinstance(from_obj, TextClause):
            # TableClause/TextClause objects are raw text SQL identifiers and as such, we cannot
            # introspect or do anything about this statement.
            return stmt

        if isinstance(from_obj, Alias):
            if isinstance(from_obj.element, Subquery):
                self.rewrite_subquery(from_obj.element)
                return stmt

            raise NotImplementedError(
                f"Unsupported object \"{(type(from_obj.element))}\" inside Alias in " f"statement.froms"
            )

        raise NotImplementedError(f"Unsupported object \"{(type(from_obj))}\" in statement.froms")

    def rewrite_from_table(self, stmt: Select, table: Table) -> Select:
        column_obj = table.columns.get(self.deleted_field_name)

        # Caveat: The automatic "bool(column_obj)" conversion actually returns
        # a truthy value of False (?), so we have to explicitly compare against None
        if column_obj is not None:
            return stmt.filter(column_obj.is_(None))

        # Soft-delete argument was not found, return unchanged statement
        return stmt
