"""Filtered copies of the subqueries and CTEs of a statement's FROM clause, and their place in the statement."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql import CompoundSelect, Join, Select, Subquery, visitors
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.elements import ColumnClause
from sqlalchemy.sql.selectable import CTE, AliasedReturnsRows, FromClause, Lateral, TextualSelect

from sqlalchemy_easy_softdelete.handler.rewriter.context import RewriteContext, Rewriter
from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals
from sqlalchemy_easy_softdelete.handler.rewriter.sa.clauses import (
    annotated_entity,
    plain,
    relationship_entity,
    subquery_in,
    ungrouped,
)


def filtered_copy(
    rewriter: Rewriter, from_obj: FromClause, memo: dict[int, FromClause | None], context: RewriteContext
) -> FromClause | None:
    """A copy of a subquery (or of an alias of one) around its rewritten statement, or None when the
    rewrite leaves the statement as it is. ``memo`` keeps the copies made for one statement."""
    key = id(from_obj)
    if key not in memo:
        element: Any = cast(AliasedReturnsRows, from_obj).element
        inner: Any
        if isinstance(from_obj, CTE) and internals.cte_alias(from_obj) is not None:
            # An alias of a CTE (cte.alias()): an alias of the CTE's filtered copy, which the WITH clause
            # renders.
            aliased_copy = filtered_copy(rewriter, internals.cte_alias(from_obj), memo, context)
            if aliased_copy is None:
                memo[key] = None
                return None
            copy = internals.copy_cte_alias(from_obj, cast(CTE, aliased_copy))
            memo[key] = copy
            return cast(FromClause, copy)
        if isinstance(from_obj, CTE):
            # The statement of a recursive CTE refers to the CTE itself (the CTE it restates).
            restated = {id(from_obj)}
            cte: Any = from_obj
            while internals.restates(cte) is not None:
                cte = internals.restates(cte)
                restated.add(id(cte))
            inner = _rewrite_unnested(rewriter, element, context.in_ctes(restated))
            unchanged = inner is element
        elif isinstance(from_obj, Subquery):
            inner = _rewrite_unnested(rewriter, element, context)
            unchanged = inner is element
        else:
            # An alias of a subquery (an alias of an aliased() entity, or of a subquery's alias, or LATERAL)
            inner = filtered_copy(
                rewriter, element, memo, context.lateral() if isinstance(from_obj, Lateral) else context
            )
            unchanged = inner is None
        filtered = None if unchanged else internals.copy_selectable(from_obj, inner)
        if isinstance(filtered, CTE) and internals.restates(filtered) is None:
            # A CTE renders once, by name: where the statement still refers to the original (in a statement
            # that includes soft-deleted rows, through an alias in a nested SELECT), SQLAlchemy renders the
            # copy, which restates it.
            internals.restate(filtered, from_obj)
        memo[key] = filtered
    return memo[key]


def _rewrite_unnested(rewriter: Rewriter, element: Any, context: RewriteContext) -> Any:
    """Rewrite the statement of a subquery or CTE of a FROM clause: it correlates to nothing, and makes copies
    of its own."""
    unnested = context.unnested()
    if isinstance(element, CompoundSelect):
        return rewriter.rewrite_compound_select(element, unnested)

    if isinstance(element, Select):
        return rewriter.rewrite_select(element, unnested)

    if isinstance(element, UpdateBase):
        # INSERT/UPDATE/DELETE ... RETURNING in a CTE: its rows are the statement's, not a table's.
        return element

    if isinstance(element, TextualSelect):
        # Raw SQL (text(...).columns(...)): there is nothing to filter, as for raw SQL in the FROM clause.
        return element

    raise NotImplementedError(f'Unsupported object "{(type(element))}" in subquery.element')


def on_copies(clause: Any, copies: dict[int, FromClause]) -> Any:
    """``clause`` with the columns of the subqueries copied in ``copies`` replaced by those of the copies."""
    if not copies:
        return clause

    def replace(element: Any, **kw: Any) -> Any:
        table = element.table if isinstance(element, ColumnClause) else None
        copy = copies.get(id(plain(table))) if table is not None else None
        return copy.c[element.key] if copy is not None else None

    return visitors.replacement_traverse(clause, {}, replace)


def place_copies(stmt: Select[Any], copies: dict[int, FromClause], join_lefts: dict[int, Any]) -> Any:
    """A copy of ``stmt`` that selects from ``copies``, keyed by the id of the subquery (or alias of a
    subquery) that each one replaces.

    Only the FROM clause changes. The statement's columns, criteria, entities and loader options keep
    referring to the original subqueries: SQLAlchemy keys result rows by those objects
    (``row._mapping[subquery.c.x]``, ``row._mapping[alias]``), and the ORM recognizes entities by
    identity. They render the same against a copy, which has the same name and columns, and
    SQLAlchemy leaves a subquery out of the FROM clause when a clone of it is there, which the copies
    are:

    - where the statement names a subquery as a FROM (``select_from()``, the target or left side of a
      join, the target of a join along a relationship), the copy takes its place, with the copy's
      columns in the join's ON clause;
    - a subquery that the statement selects from only through its columns, criteria or entities gets
      its copy added to the FROM list, which SQLAlchemy reads before them. So does the left side of a
      join along a relationship (``join_from(alias, alias.rel)``), which SQLAlchemy finds there. A
      FROM list changes how SQLAlchemy picks the left side of a join that does not give one, so such
      joins get the one SQLAlchemy chose for ``stmt``, from ``join_lefts``;
    - a SELECT nested in the statement (a scalar subquery, ``IN``, ``EXISTS``) that selects from a
      subquery gets its copy too, as a statement of its own.
    """
    return _CopyPlacer(copies, join_lefts).place(stmt)


class _CopyPlacer:
    """``place_copies`` for one statement: ``placed`` holds the copies it has put in the statement's FROM
    elements and joins; the others join its FROM list."""

    def __init__(self, copies: dict[int, FromClause], join_lefts: dict[int, Any]) -> None:
        self.copies = copies
        self.join_lefts = join_lefts
        self.placed: set[int] = set()

    def place(self, stmt: Select[Any]) -> Any:
        parts: dict[str, Any] = {
            "raw_columns": list(self._place_in_all(internals.raw_columns(stmt))),
            "where_criteria": self._place_in_all(internals.where_criteria(stmt)),
            "having_criteria": self._place_in_all(internals.having_criteria(stmt)),
            "order_by": self._place_in_all(internals.order_by(stmt)),
            "group_by": self._place_in_all(internals.group_by(stmt)),
            "from_list": tuple(self._swap_from(from_obj) for from_obj in internals.from_list(stmt)),
            "joins": self._swap_joins(internals.joins(stmt)),
        }
        set_aside = internals.set_aside_entities(stmt)
        if set_aside:
            # with_only_columns() sets the statement's earlier joins aside.
            memoized = []
            for entities in set_aside:
                entity_joins = internals.joins(entities)
                new_joins = self._swap_joins(entity_joins)
                if new_joins is not entity_joins:
                    entities = internals.with_joins(entities, new_joins)
                memoized.append(entities)
            parts["set_aside_entities"] = tuple(memoized)
        unplaced = tuple(copy for key, copy in self.copies.items() if key not in self.placed)
        if unplaced:
            parts["from_list"] = parts["from_list"] + unplaced
        # The new statement is this function's own copy: setting its parts modifies nothing else.
        return internals.copy_statement(stmt, **parts)

    def _copy_of(self, from_obj: Any) -> Any:
        if from_obj is None:
            return None
        key = id(plain(ungrouped(from_obj)))
        copy = self.copies.get(key)
        if copy is not None:
            self.placed.add(key)
        return copy

    def _swap_from(self, from_obj: Any) -> Any:
        copy = self._copy_of(from_obj)
        if copy is not None:
            return copy
        if isinstance(plain(ungrouped(from_obj)), Join):
            # A join written with Core, which holds subqueries.
            return visitors.replacement_traverse(
                from_obj,
                {},
                lambda element, **kw: self._copy_of(element) if subquery_in(element) is not None else None,
            )
        return from_obj

    def _swap_join(self, join: tuple[Any, ...]) -> tuple[Any, ...]:
        target, onclause, from_, flags = join
        # A join along a relationship goes to an entity: one over the copy takes the place of the
        # original (SQLAlchemy builds such an entity itself for a selectable, with a deprecation warning).
        if isinstance(target, QueryableAttribute) and onclause is None:
            # join(Parent.children): to the relationship's target, or its of_type() entity
            entity = relationship_entity(target)
            copy = self._copy_of(entity.selectable) if entity is not None else None
            new_target, new_onclause = (
                (target, onclause) if copy is None else (internals.entity_over(entity, copy), target)
            )
        elif isinstance(onclause, QueryableAttribute):
            # join(entity, Parent.children): to the given entity
            copy = self._copy_of(target)
            entity = annotated_entity(target) or relationship_entity(onclause)
            new_target, new_onclause = (
                (target, onclause) if copy is None else (internals.entity_over(entity, copy), onclause)
            )
        else:
            new_target, new_onclause = self._swap_from(target), self._place_in_nested(onclause)
        if isinstance(new_onclause, QueryableAttribute) or isinstance(target, QueryableAttribute):
            # SQLAlchemy joins along a relationship from the attribute's own entity, which it looks for
            # in the FROM list: the copy of a left side that is a subquery goes there.
            new_from = (
                None
                if from_ is not None
                and plain(ungrouped(from_)) is not None
                and id(plain(ungrouped(from_))) in self.copies
                else from_
            )
        else:
            new_from = self._swap_from(self.join_lefts.get(id(join), from_))
        if new_target is target and new_onclause is onclause and new_from is from_:
            return join
        if new_onclause is not None and not isinstance(new_onclause, QueryableAttribute):
            # Columns of the original would let SQLAlchemy join from the original as well.
            new_onclause = on_copies(new_onclause, self.copies)
        return (new_target, new_onclause, new_from, flags)

    def _swap_joins(self, stmt_joins: tuple[Any, ...]) -> tuple[Any, ...]:
        swapped = tuple(self._swap_join(join) for join in stmt_joins)
        return stmt_joins if all(new is old for new, old in zip(swapped, stmt_joins)) else swapped

    def _place_in_nested(self, clause: Any) -> Any:
        """``clause``, with the SELECTs nested in it that select from a copied subquery selecting from
        the copy; ``clause`` itself when there are none (columns keep their identity)."""
        if clause is None or not any(_selects_from_copies(child, self.copies) for child in visitors.iterate(clause)):
            return clause

        def replace(element: Any, **kw: Any) -> Any:
            if _selects_from_copies(element, self.copies):
                return place_copies(element, self.copies, {})
            return None

        return visitors.replacement_traverse(clause, {}, replace)

    def _place_in_all(self, elements: Any) -> Any:
        placed_elements = tuple(self._place_in_nested(element) for element in elements)
        return elements if all(new is old for new, old in zip(placed_elements, elements)) else placed_elements


def _selects_from_copies(element: Any, copies: dict[int, FromClause]) -> bool:
    """Whether ``element`` is a SELECT that selects from (or refers to columns of) a subquery copied in
    ``copies``."""
    if not isinstance(element, Select):
        return False
    for child in visitors.iterate(element):
        table = child.table if isinstance(child, ColumnClause) else child if isinstance(child, FromClause) else None
        if table is not None and id(plain(ungrouped(table))) in copies:
            return True
    return False
