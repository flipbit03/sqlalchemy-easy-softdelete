"""SQLAlchemy's private API, as the rewriter uses it: the one place that reads or writes private attributes of
SQLAlchemy objects, and that knows how SQLAlchemy releases differ.

SQLAlchemy has no public API to read a SELECT's parts (its joins, its criteria, its loader options) or to make a
copy of a statement with some of them replaced. These attributes are those of the statements' ``_traverse_internals``
and ``_generate()``, stable across the supported releases (2.0.28 to 2.1). A new SQLAlchemy release is checked
against this module, and its version differences are listed here:

=============================  =====================================================================================
SQLAlchemy                     Difference, and where it is handled
=============================  =====================================================================================
before 2.0.30                  ``ORMExecuteState.is_select`` is False for ``select().from_statement()``, also inside a
                               ``lambda_stmt()``: the hook checks the statement (``resolved``).
before 2.0.32                  ``Select.compare()`` raises ``TypeError`` comparing a SELECT with ``correlate_except()``
                               (``any()``, ``has()``) to one without: ``same_expression``.
2.1                            ``get_final_froms()`` refuses a nested SELECT whose own nested SELECT would correlate to
                               all of its FROM elements: the planner falls back to the FROM elements without criteria.
2.1.0                          Loader criteria also apply to the correlated classes of a nested SELECT: a HAVING SELECT
                               that GROUP BY also has is left to the criteria (see ``transform.nested``).
=============================  =====================================================================================
"""

from __future__ import annotations

from collections.abc import Callable
from operator import attrgetter
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm.util import AliasedClass, AliasedInsp, LoaderCriteriaOption
from sqlalchemy.sql import CompoundSelect, Select, visitors
from sqlalchemy.sql.selectable import CTE, FromClause

# --- The parts of a SELECT --------------------------------------------------------------------------------------

raw_columns: Callable[[Any], Any] = attrgetter("_raw_columns")
"""The columns and entities a SELECT selects, as given."""

from_list: Callable[[Any], Any] = attrgetter("_from_obj")
"""The FROM elements of ``select_from()``."""

joins: Callable[[Any], Any] = attrgetter("_setup_joins")
"""The joins of ``join()``: ``(target, onclause, from, flags)`` each."""

where_criteria: Callable[[Any], Any] = attrgetter("_where_criteria")
having_criteria: Callable[[Any], Any] = attrgetter("_having_criteria")
order_by: Callable[[Any], Any] = attrgetter("_order_by_clauses")
group_by: Callable[[Any], Any] = attrgetter("_group_by_clauses")

set_aside_entities: Callable[[Any], Any] = attrgetter("_memoized_select_entities")
"""The entities and joins ``with_only_columns()`` sets aside: objects with their own ``joins`` and ``raw_columns``."""

loader_options: Callable[[Any], Any] = attrgetter("_with_options")
"""The statement's options: loader options (``Load``) and others."""


def extra_criteria(loader: Any) -> tuple[Any, ...]:
    """The criteria a loader option's path gives its relationship (``joinedload(Parent.children.and_(...))``)."""
    return tuple(getattr(loader, "_extra_criteria", ()))


_PARTS = {
    "raw_columns": "_raw_columns",
    "from_list": "_from_obj",
    "joins": "_setup_joins",
    "where_criteria": "_where_criteria",
    "having_criteria": "_having_criteria",
    "order_by": "_order_by_clauses",
    "group_by": "_group_by_clauses",
    "set_aside_entities": "_memoized_select_entities",
    "loader_options": "_with_options",
}


def copy_statement(stmt: Any, **parts: Any) -> Any:
    """A copy of ``stmt`` (a SELECT) with ``parts`` replaced, by the names of the readers above. The copy is the
    caller's own: replacing its parts changes nothing else."""
    copy = stmt._generate()
    for name, value in parts.items():
        setattr(copy, _PARTS[name], value)
    return copy


def generate(stmt: Any) -> Any:
    """A copy of a statement (a union, a ``from_statement()``) whose public attributes the caller then sets."""
    return stmt._generate()


def with_joins(entities: Any, new_joins: Any) -> Any:
    """A copy of set-aside entities (see ``set_aside_entities``) with ``new_joins``."""
    copy = entities._clone()
    copy._setup_joins = new_joins
    return copy


def cache_key(stmt: Any) -> Any:
    """The statement's cache key, or None when it has none."""
    return stmt._generate_cache_key()


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


def correlation(stmt: Any) -> tuple[Any, Any, bool]:
    """What ``correlate()`` and ``correlate_except()`` gave a SELECT, and whether it auto-correlates."""
    return stmt._correlate, stmt._correlate_except, stmt._auto_correlate


# --- Elements -----------------------------------------------------------------------------------------------------


def annotations_of(element: Any) -> Any:
    """The ORM's annotations of an element (``parententity``, ``parentmapper``, ...); empty when it has none."""
    return getattr(element, "_annotations", {})


def deannotate(element: Any) -> Any:
    """``element`` without the ORM's annotations: the object the statement was built with."""
    return element._deannotate()


def from_objects(element: Any) -> Any:
    """The FROM elements an element (a column, a criterion) refers to."""
    return element._from_objects


def is_clone_of(element: Any) -> Any:
    """The element ``element`` is a clone of (the first of the clones), or None."""
    return getattr(element, "_is_clone_of", None)


def clone(element: Any) -> Any:
    return element._clone()


def of_type(attribute: Any) -> Any:
    """The ``of_type()`` entity of a relationship attribute, or None."""
    return attribute._of_type


def resolved(stmt: Any) -> Any:
    """The statement a ``lambda_stmt()`` stands for; ``stmt`` itself for any other statement."""
    return getattr(stmt, "_resolved", stmt)


def lambda_with_statement(element: Any, statement: Any, token: Any) -> Any:
    """A copy of a lambda statement that stands for ``statement``, with ``token`` in its cache key.

    SQLAlchemy caches the compiled form of a lambda statement by the lambda's code and closure, not by its resulting
    statement, so the copy would share the compiled form of the statement without soft-delete filtering (from a Core
    execution of the same lambda, which the hook does not rewrite). The token in the cache key keeps the two apart.
    """
    copy = element._clone()
    copy._resolved = statement
    if isinstance(copy.closure_cache_key, tuple):
        copy.closure_cache_key = copy.closure_cache_key + (token,)
    return copy


def execution_option(stmt: Any, name: str) -> Any:
    """An execution option of a statement being compiled (its own options, not those of an execution)."""
    return stmt._execution_options.get(name)


# --- CTEs and copies of subqueries -------------------------------------------------------------------------------


def cte_alias(cte: Any) -> Any:
    """The CTE an alias of a CTE (``cte.alias()``) is an alias of, or None."""
    return cte._cte_alias


def restates(element: Any) -> Any:
    """The CTE a CTE restates (a recursive CTE's union restates the CTE it was made from), or None; None for any
    other element."""
    return getattr(element, "_restates", None)


def restate(copy: Any, original: Any) -> None:
    """Make the CTE ``copy`` restate ``original``: SQLAlchemy renders a CTE once, by name, and renders the copy
    where the statement still refers to the original."""
    copy._restates = original


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


def copy_cte_alias(alias: Any, aliased_copy: Any) -> Any:
    """A copy of an alias of a CTE (``cte.alias()``) that is an alias of ``aliased_copy``, the CTE's filtered copy,
    which the WITH clause renders."""
    copy = alias._clone()
    copy._is_clone_of = alias
    copy._cte_alias = aliased_copy
    copy.element = aliased_copy.element
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

_FILTERED_MARK = "_soft_delete_filtered_"


def mark_filtered(stmt: Any) -> None:
    """Mark a nested SELECT the rewrite filtered: the loader criteria leave it out (see ``is_marked_filtered``)."""
    setattr(stmt, _FILTERED_MARK, True)


def is_marked_filtered(stmt: Any) -> bool:
    return bool(getattr(stmt, _FILTERED_MARK, False))


def same_expression(element: Any, other: Any) -> bool:
    """Whether two SELECTs are the same expression (``compare()``)."""
    try:
        return bool(element.compare(other))
    except TypeError:
        # SQLAlchemy before 2.0.32 cannot compare a SELECT with correlate_except() (any(), has()) to one
        # without: they are not the same.
        return False


def without_criteria(stmt: Any) -> Any:
    """A copy of a SELECT without its WHERE and HAVING criteria."""
    bare = stmt._generate()
    bare._where_criteria = ()
    bare._having_criteria = ()
    return bare


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

LOADER_CRITERIA_TRAVERSE_INTERNALS = LoaderCriteriaOption._traverse_internals
"""What makes up the cache key of a ``with_loader_criteria()`` option, which a subclass extends."""
