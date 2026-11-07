"""Pure helpers over SQLAlchemy clauses and FROM elements: what a FROM element is, wraps, or is made of."""

from __future__ import annotations

import itertools
from typing import Any

from sqlalchemy import Table, inspect
from sqlalchemy.orm import Mapper, RelationshipProperty
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql import Alias, CompoundSelect, Join, Select, Subquery, visitors
from sqlalchemy.sql.annotation import Annotated
from sqlalchemy.sql.selectable import CTE, FromClause, FromGrouping, Lateral

from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals


def plain(element: Any) -> Any:
    """``element`` without the ORM's annotations: the object the statement was built with."""
    return internals.deannotate(element) if isinstance(element, Annotated) else element


def ungrouped(from_obj: Any) -> Any:
    """The FROM element inside parentheses (a JOIN nested on the right of another JOIN is grouped)."""
    while isinstance(from_obj, FromGrouping):
        from_obj = from_obj.element
    return from_obj


def subquery_in(selectable: Any) -> Subquery | CTE | None:
    """The subquery (or CTE) ``selectable`` is, or wraps in aliases, LATERAL and parentheses, if any."""
    while True:
        if isinstance(selectable, (Subquery, CTE)):
            return selectable
        if isinstance(selectable, (Alias, Lateral, FromGrouping)):
            selectable = selectable.element
            continue
        return None


def aliased_table(from_obj: Any) -> Table | None:
    """The table ``from_obj`` is an alias of (through any number of aliases), if it is one."""
    if not isinstance(from_obj, Alias):
        return None
    while isinstance(from_obj, Alias):
        from_obj = from_obj.element
    return from_obj if isinstance(from_obj, Table) else None


def cte_root(selectable: Any) -> Any:
    """The CTE an alias of a CTE (``cte.alias()``) refers to, through any number of aliases; ``selectable``
    itself otherwise."""
    while isinstance(selectable, CTE) and internals.cte_alias(selectable) is not None:
        selectable = internals.cte_alias(selectable)
    return selectable


def join_leaves(from_obj: Any) -> list[Any]:
    """The tables, subqueries and other FROM elements a join is made of, in order."""
    from_obj = ungrouped(from_obj)
    if isinstance(from_obj, Join):
        return join_leaves(from_obj.left) + join_leaves(from_obj.right)
    return [from_obj]


def inner_joined_leaves(from_obj: Any) -> list[Any]:
    """The leaves of a join that each of its rows has a row of: those not on the right of a LEFT OUTER JOIN."""
    from_obj = ungrouped(from_obj)
    if not isinstance(from_obj, Join):
        return [from_obj]
    left = inner_joined_leaves(from_obj.left)
    if from_obj.isouter and not from_obj.full:
        return left
    return left + inner_joined_leaves(from_obj.right)


def joins_of(stmt: Any) -> Any:
    """The joins of ``stmt``, including those ``with_only_columns()`` set aside."""
    return itertools.chain(
        internals.joins(stmt), *(internals.joins(entities) for entities in internals.set_aside_entities(stmt))
    )


def annotated_entity(element: Any) -> Any:
    """The entity (mapper or ``aliased()`` entity) an ORM-annotated element belongs to, if any."""
    return internals.annotations_of(element).get("parententity") if isinstance(element, Annotated) else None


def relationship_entity(attr: QueryableAttribute[Any]) -> Any:
    """The entity a join along a relationship attribute joins to: its ``of_type()`` entity, or the
    relationship's own target (a class, or an ``aliased()`` class)."""
    prop = attr.property
    if not isinstance(prop, RelationshipProperty):
        return None
    of_type = internals.of_type(attr)
    entity: Any = of_type if of_type is not None else prop.entity
    return inspect(entity)


def relationship_target(attr: QueryableAttribute[Any]) -> Any:
    """The selectable a join along a relationship attribute joins to."""
    entity = relationship_entity(attr)
    return entity.selectable if entity is not None else None


def join_target_selectable(target: Any) -> Any:
    """The selectable a join of a statement joins to: that of a relationship's target (or its ``of_type()``
    entity), of an entity, or a table."""
    if isinstance(target, QueryableAttribute):
        entity = relationship_entity(target)
        return entity.selectable if entity is not None else None
    insp = inspect(target, raiseerr=False)
    return getattr(insp, "selectable", None)


def selects_from_mapper(element: Any, mapper: Mapper[Any]) -> bool:
    """Whether an ORM element selects from its mapper's own tables (its entity, or one of its columns),
    rather than from a subquery whose columns carry the mapper's annotations."""
    table = element if isinstance(element, FromClause) else getattr(element, "table", None)
    if table is None:
        return False
    table = plain(table)
    return any(table is mapped for mapped in mapper.tables)


def add_selects(element: Any, found: set[int]) -> None:
    """Add to ``found`` the ids of the SELECTs in ``element``, and of those they are clones of."""
    for child in visitors.iterate(element):
        if isinstance(child, (Select, CompoundSelect)):
            found.add(id(child))
            original = internals.is_clone_of(child)
            if original is not None:
                found.add(id(original))


def column_property_selects(mappers: list[Mapper[Any]]) -> list[Any]:
    """The SELECTs in the ``column_property()`` expressions of ``mappers`` and of their subclasses."""
    found: dict[int, Any] = {}
    for mapper in mappers:
        for each in mapper.self_and_descendants:
            for prop in each.column_attrs:
                for column in prop.columns:
                    for select in internals.top_selects(column):
                        found.setdefault(id(select), select)
    return list(found.values())
