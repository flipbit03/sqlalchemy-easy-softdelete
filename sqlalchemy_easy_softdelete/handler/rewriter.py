from sqlalchemy import Table
from sqlalchemy.orm.util import _ORMJoin
from sqlalchemy.sql import Alias, Select, TableClause


def rewrite_from_table(stmt: Select, table: Table, deleted_field_name: str) -> Select:
    column_obj = table.columns.get(deleted_field_name)

    # Caveat: The automatic "bool(column_obj)" conversion actually returns
    # a truthy value of False (?), so we have to explicitly compare against None
    if column_obj is not None:
        return stmt.filter(column_obj.is_(None))

    # Soft-delete argument was not found, return unchanged statement
    return stmt


def analyze_from(stmt: Select, from_obj: Table | _ORMJoin, deleted_field_name: str) -> Select:
    if isinstance(from_obj, Table):
        return rewrite_from_table(stmt, from_obj, deleted_field_name)

    if isinstance(from_obj, _ORMJoin):
        # _ORMJOIN contains information about two tables: 'left' and 'right'. Check both.
        left_adapted_stmt = rewrite_from_table(stmt, from_obj.left, deleted_field_name)
        right_adapted_stmt = rewrite_from_table(left_adapted_stmt, from_obj.right, deleted_field_name)
        return right_adapted_stmt

    if isinstance(from_obj, Alias):
        # Recursively analyze and modify the Alias object's Select statement
        from_obj.element.element = rewrite_select(from_obj.element.element, deleted_field_name)
        return stmt

    if isinstance(from_obj, TableClause):
        # TableClause objects are raw text SQL identifiers and as such, we cannot
        # introspect or do anything about this statement.
        return stmt

    raise NotImplementedError(f"Unsupported object \"{(type(from_obj))}\" in statement.froms")


def rewrite_select(stmt: Select, deleted_field_name) -> Select:
    for from_obj in stmt.get_final_froms():
        stmt = analyze_from(stmt, from_obj, deleted_field_name)

    return stmt
