"""Helpers over SQLAlchemy clauses and FROM elements."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from sqlalchemy import Table, inspect
from sqlalchemy.orm import Mapper, RelationshipProperty
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.orm.util import AliasedClass, AliasedInsp
from sqlalchemy.sql import Alias, CompoundSelect, Join, Select, Subquery, visitors
from sqlalchemy.sql.annotation import Annotated
from sqlalchemy.sql.selectable import CTE, FromClause, FromGrouping, Lateral


def plain(element: Any) -> Any:
    """``element`` without the ORM's annotations: the object the statement was built with."""
    return element._deannotate() if isinstance(element, Annotated) else element


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
    while isinstance(selectable, CTE) and selectable._cte_alias is not None:
        selectable = selectable._cte_alias
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
    return itertools.chain(stmt._setup_joins, *(entities._setup_joins for entities in stmt._memoized_select_entities))


def annotated_entity(element: Any) -> Any:
    """The entity (mapper or ``aliased()`` entity) an ORM-annotated element belongs to, if any."""
    return getattr(element, "_annotations", {}).get("parententity") if isinstance(element, Annotated) else None


def relationship_entity(attr: QueryableAttribute[Any]) -> Any:
    """The entity a join along a relationship attribute joins to: its ``of_type()`` entity, or the
    relationship's own target (a class, or an ``aliased()`` class)."""
    prop = attr.property
    if not isinstance(prop, RelationshipProperty):
        return None
    of_type = attr._of_type
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
            original = getattr(child, "_is_clone_of", None)
            if original is not None:
                found.add(id(original))


def column_property_selects(mappers: list[Mapper[Any]]) -> list[Any]:
    """The SELECTs in the ``column_property()`` expressions of ``mappers`` and of their subclasses."""
    found: dict[int, Any] = {}
    for mapper in mappers:
        for each in mapper.self_and_descendants:
            for prop in each.column_attrs:
                for column in prop.columns:
                    for select in top_selects(column):
                        found.setdefault(id(select), select)
    return list(found.values())


def final_froms_without_loader_options(stmt: Any) -> Any:
    """The FROM list of ``stmt`` as written, before eager loading changes it.

    Eager loading adds joins to aliases of the loaded tables, which ``with_loader_criteria`` filters,
    and around a LIMIT it moves the whole statement into a subquery, which takes the statement's
    filters along. Leaving the loader options out also spares the analysis the eager loaders' setup.

    The analysis runs on a copy: SQLAlchemy's ORM compilation stores its compile options on the
    statement it compiles, which changes the statement's cache key.
    """
    bare = stmt._generate()
    bare._with_options = ()
    return bare.get_final_froms()


# --- Elements -----------------------------------------------------------------------------------------------------


# --- CTEs and copies of subqueries -------------------------------------------------------------------------------


def copy_selectable(selectable: Any, element: Any) -> Any:
    """A copy of a subquery or alias (same name, so the same SQL) around a different element, which
    SQLAlchemy knows as a clone of the original."""
    copy = selectable._clone()
    # _clone() makes a clone of a clone a clone of the first object: the copy must be one of the
    # caller's own object, whose columns the statement selects, for SQLAlchemy to leave that one out.
    copy._is_clone_of = selectable
    copy.element = element
    copy._reset_column_collection()
    return copy


def cte_lineage(selectable: Any) -> set[int]:
    """The ids of a CTE, of the CTE it is an alias of, and of those they restate or are clones of; empty for
    anything that is not a CTE."""
    found: set[int] = set()
    stack: list[Any] = [selectable] if isinstance(selectable, CTE) else []
    while stack:
        cte = stack.pop()
        if cte is None or id(cte) in found:
            continue
        found.add(id(cte))
        stack.extend((cte._cte_alias, cte._restates, getattr(cte, "_is_clone_of", None)))
    return found


# --- Entities -----------------------------------------------------------------------------------------------------


def in_polymorphic_alias(element: Any) -> bool:
    """Whether a loader path element is a ``with_polymorphic()`` entity or one of its subclass entities, which a
    rebuilt path cannot name again."""
    if not isinstance(element, AliasedInsp):
        return False
    return bool(getattr(element, "_is_with_polymorphic", False)) or element._base_alias() is not element


def entity_over(insp: Any, selectable: FromClause) -> Any:
    """A join target like the entity ``insp`` (a mapper, or an ``aliased()`` entity), over ``selectable``:
    an ``aliased()`` entity with the same settings."""
    if isinstance(insp, AliasedInsp):
        entity = AliasedClass(
            insp.mapper,
            alias=selectable,
            name=insp.name,
            with_polymorphic_mappers=insp.with_polymorphic_mappers if insp._is_with_polymorphic else (),
            with_polymorphic_discriminator=insp.polymorphic_on,
            adapt_on_names=insp._adapt_on_names,
            use_mapper_path=insp._use_mapper_path,
            represents_outer_join=insp.represents_outer_join,
        )
    else:
        entity = AliasedClass(insp.mapper, alias=selectable)
    return inspect(entity).__clause_element__()


# --- Nested SELECTs ---------------------------------------------------------------------------------------------


def same_expression(element: Any, other: Any) -> bool:
    """Whether two SELECTs are the same expression (``compare()``)."""
    try:
        return bool(element.compare(other))
    except TypeError:
        # SQLAlchemy before 2.0.32 cannot compare a SELECT with correlate_except() (any(), has()) to one
        # without: they are not the same.
        return False


FILTERED_MARK = "_soft_delete_filtered_"
"""Set on a nested SELECT that the rewrite filtered: the loader criteria leave it out (see ``SoftDeleteCriteria``)."""

_NO_REPLACEMENT = "no_replacement_traverse"


def top_selects(clause: Any) -> Any:
    """The SELECTs in ``clause`` that are not nested in another SELECT of it."""
    stack = [clause]
    while stack:
        element = stack.pop()
        if isinstance(element, (Select, CompoundSelect)):
            yield element
            continue
        stack.extend(element.get_children())


def replace_selects(clause: Any, replace: Callable[..., Any]) -> Any:
    """``visitors.replacement_traverse(clause, {}, replace)``, which also goes into the criterion of ``any()`` and
    ``has()``: SQLAlchemy annotates it so that replacement traversals leave it alone. A copy with its SELECTs
    replaced keeps the annotation, for SQLAlchemy's own adaptation of the statement."""

    def shielded(element: Any) -> bool:
        return _NO_REPLACEMENT in element._annotations and any(True for _ in top_selects(element))

    def unshielded(element: Any) -> Any:
        bare = element._deannotate(values=(_NO_REPLACEMENT,))
        return replace_selects(bare, replace)._annotate({_NO_REPLACEMENT: True})

    def visit(element: Any, **kw: Any) -> Any:
        new = replace(element, **kw)
        if new is not None:
            return new
        if any(shielded(child) for child in element.get_children()):
            # The criterion of any() and has() is in the condition of the EXISTS they make: in an and_(), or in
            # parentheses. The element is copied as replacement_traverse copies one, with the criterion unshielded.
            copy = element._clone()
            copy._copy_internals(
                clone=lambda child, **kw: unshielded(child) if shielded(child) else replace_selects(child, replace)
            )
            return copy
        return None

    if shielded(clause):
        return unshielded(clause)
    return visitors.replacement_traverse(clause, {}, visit)


# --- Loader criteria ----------------------------------------------------------------------------------------------
