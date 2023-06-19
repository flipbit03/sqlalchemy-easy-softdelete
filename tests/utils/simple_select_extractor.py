"""
Extracts SIMPLE SELECT STATEMENTS from a query

We consider SIMPLE SELECT STATEMENTS to be those that their froms are tables, and not subqueries.
"""
from typing import Union

from sqlalchemy.orm.util import _ORMJoin
from sqlalchemy.sql.schema import Table
from sqlalchemy.sql.selectable import CompoundSelect, Join, Select, Subquery


def is_simple_join(j: Union[Join, _ORMJoin]) -> bool:
    left_simple, right_simple = False, False

    if isinstance(j.left, Table):
        left_simple = True
    elif isinstance(j.left, _ORMJoin) or isinstance(j.left, Join):
        left_simple = is_simple_join(j.left)

    if isinstance(j.right, Table):
        right_simple = True
    elif isinstance(j.right, _ORMJoin) or isinstance(j.right, Join):
        right_simple = is_simple_join(j.right)

    return left_simple and right_simple


def is_simple_select(s: Union[Select, Subquery, CompoundSelect]) -> bool:
    if isinstance(s, CompoundSelect):
        return False

    if isinstance(s, Subquery):
        return False

    if not isinstance(s.froms, list):
        raise NotImplementedError(f"statement.froms is not a list! type -> \"{(type(s.froms))}\"!")

    for from_obj in s.froms:
        if isinstance(from_obj, Table):
            continue
        elif isinstance(from_obj, Subquery):
            return False
        elif isinstance(from_obj, _ORMJoin) or isinstance(from_obj, Join):
            if is_simple_join(from_obj):
                continue
            return False
        else:
            raise NotImplementedError(f"Unsupported froms type \"{(type(from_obj))}\"!")

    return True


def extract_simple_selects(statement: Union[Select, CompoundSelect]) -> list[Select]:
    if is_simple_select(statement):
        return [statement]

    if isinstance(statement, CompoundSelect):
        extraced_selects = []
        for select in statement.selects:
            extraced_selects.extend(extract_simple_selects(select))
        return extraced_selects

    for from_obj in statement.froms:
        if isinstance(from_obj, Table):
            continue
        elif isinstance(from_obj, Subquery):
            return extract_simple_selects(from_obj.element)
        else:
            print("oh no")

    raise NotImplementedError(f"Should not reach this point! statement.froms -> \"{statement.froms}\"!")
