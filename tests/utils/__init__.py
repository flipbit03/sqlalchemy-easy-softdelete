from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList, Null
from sqlalchemy.sql.schema import Table
from sqlalchemy.sql.selectable import Select

from tests.utils.simple_select_extractor import extract_simple_selects


def extract_binary_expressions_from_where(whereclause) -> tuple[BinaryExpression]:
    if isinstance(whereclause, BinaryExpression):
        return (whereclause,)

    if isinstance(whereclause, BooleanClauseList):
        clauses = tuple(whereclause.clauses)
        # Make sure we only have BinaryExpressions
        assert all(isinstance(c, BinaryExpression) for c in clauses)

        return tuple(whereclause.clauses)

    raise NotImplementedError(f"Unsupported whereclause type \"{(type(whereclause))}\"!")


def is_soft_delete_filter(b: BinaryExpression, tables: list[Table], deleted_field: str):
    return b.left.table in tables and b.left.name == deleted_field and isinstance(b.right, Null)


def is_simple_select_doing_soft_delete_filtering(stmt: Select, tables: set[Table], deleted_field: str) -> bool:
    # Check if query is disabled for soft-deletion
    opts = stmt.get_execution_options()
    if opts and opts.get("include_deleted"):
        # Skip checking in this query
        return True

    # if we don't have a where clause, we can't be filtering for soft-deleted
    # Caveat: We need to compare with None, since and whereclause usually does not have a __bool__ method
    if stmt.whereclause is None:
        return False

    binary_expressions = extract_binary_expressions_from_where(stmt.whereclause)

    found_tables = set()
    for binary_expression in binary_expressions:
        if is_soft_delete_filter(binary_expression, tables, deleted_field):
            found_tables.add(binary_expression.left.table)

    if found_tables == tables:
        return True

    return False


def is_filtering_for_softdeleted(statement: Select, tables: set[Table], deleted_field: str = "deleted_at") -> bool:
    selects = extract_simple_selects(statement)

    # Make sure all extracted selects are doing soft-delete filtering
    return all([is_simple_select_doing_soft_delete_filtering(s, tables, deleted_field) for s in selects])
