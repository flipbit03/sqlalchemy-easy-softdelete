"""Building the rewritten statement from a ``SelectPlan``: filtered copies of subqueries, nested SELECTs rewritten,
loader criteria, and the soft-delete filters in ON and WHERE clauses."""

from __future__ import annotations

import itertools
from typing import Any, cast

from sqlalchemy import and_
from sqlalchemy.orm import Mapper
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.orm.util import AliasedInsp, LoaderCriteriaOption
from sqlalchemy.sql import CompoundSelect, Join, Select, Subquery, visitors
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.elements import ColumnClause
from sqlalchemy.sql.selectable import CTE, AliasedReturnsRows, FromClause, Lateral, TextualSelect
from sqlalchemy.sql.visitors import InternalTraversal

from sqlalchemy_easy_softdelete.handler.rewriter.analysis import (
    FromScan,
    correlated_froms,
    from_ids,
    is_listed,
    joined_secondary_options,
    listed_selects,
)
from sqlalchemy_easy_softdelete.handler.rewriter.clauses import (
    FILTERED_MARK,
    add_selects,
    annotated_entity,
    copy_selectable,
    entity_over,
    join_leaves,
    join_target_selectable,
    joins_of,
    plain,
    relationship_entity,
    replace_selects,
    subquery_in,
    top_selects,
    ungrouped,
)
from sqlalchemy_easy_softdelete.handler.rewriter.plan import (
    Criteria,
    FromAnalysis,
    RewriteContext,
    Rewriter,
    SelectPlan,
)
from sqlalchemy_easy_softdelete.handler.rewriter.policy import PICKLE_MODULE, Configuration, SoftDeletePolicy
from sqlalchemy_easy_softdelete.hook import IgnoredTable


def with_criteria(policy: SoftDeletePolicy, stmt: Select[Any], criteria: Criteria, holds_selects: bool = True) -> Any:
    """``stmt`` with ``with_loader_criteria`` options for ``criteria`` (``SoftDeleteCriteria``, which leaves
    out the SELECTs that disable soft-delete filtering). ``holds_selects`` is whether ``stmt`` holds SELECTs,
    which are looked through for those."""
    if not criteria:
        return stmt
    disabled, signature = disabled_selects(policy, stmt, holds_selects)
    options = []
    for entity, include_aliases in criteria.items():
        if hasattr(entity, policy.deleted_field_name):
            where, configuration = policy.is_not_deleted, None
        else:
            where, configuration = policy.mapped_subquery_condition(entity), policy.configuration()
        options.append(
            SoftDeleteCriteria(
                entity,
                where,
                include_aliases=include_aliases,
                disable_option_name=policy.disable_soft_delete_option_name,
                disabled=disabled,
                disabled_signature=signature,
                configuration=configuration,
            )
        )
    return stmt.options(*options)


def disabled_selects(
    policy: SoftDeletePolicy, stmt: Any, holds_selects: bool = True
) -> tuple[frozenset[int], tuple[int, ...]]:
    """The ids of the SELECTs the eager loading criteria leave out (see ``SoftDeleteCriteria``): those of
    ``stmt`` that disable soft-delete filtering, anywhere in it, with what is nested in them and the SELECTs
    they are clones of, and those of the column_property() expressions of the classes it loads; and where in
    ``stmt`` the former are, among its SELECTs, which tells statements of the same shape apart."""
    disabled: set[int] = set()
    places: list[int] = []
    if holds_selects:
        selects = (element for element in visitors.iterate(stmt) if isinstance(element, (Select, CompoundSelect)))
        for place, select in enumerate(selects):
            if select is not stmt and id(select) not in disabled and policy.is_disabled(select):
                places.append(place)
                add_selects(select, disabled)
    if isinstance(stmt, Select):
        # Those of column_property() expressions belong to the mapping, the same for every statement.
        mappers = [
            insp.mapper
            for insp in (annotated_entity(column) for column in stmt._raw_columns if isinstance(column, FromClause))
            if isinstance(insp, (Mapper, AliasedInsp))
        ]
        for mapper in mappers:
            disabled.update(policy.disabled_selects_of(mapper))
    return frozenset(disabled), tuple(places)


class SoftDeleteCriteria(LoaderCriteriaOption):
    """``with_loader_criteria()`` for the soft-delete filter of eager loading and of nested SELECTs, which
    SQLAlchemy applies to every SELECT of the class compiled with the statement. It leaves out the SELECTs in
    ``disabled`` (by id): those that disable soft-delete filtering, anywhere in the statement, and the SELECTs
    nested in them, such as a subquery that counts soft-deleted rows on purpose.

    ``disabled`` holds objects of one statement, which cache keys leave out, and so do the SELECTs' execution
    options. ``disabled_signature``, where those SELECTs are among the statement's, is part of the cache key: two
    statements of the same shape that disable filtering in different SELECTs never share compiled SQL.
    """

    _traverse_internals = [
        *LoaderCriteriaOption._traverse_internals,
        ("disable_option_name", InternalTraversal.dp_string),
        ("disabled_signature", InternalTraversal.dp_plain_obj),
    ]

    def __init__(
        self,
        entity: Any,
        where_criteria: Any,
        *,
        include_aliases: bool,
        disable_option_name: str,
        disabled: frozenset[int] = frozenset(),
        disabled_signature: tuple[int, ...] = (),
        configuration: Configuration | None = None,
    ) -> None:
        super().__init__(entity, where_criteria, include_aliases=include_aliases)
        self.disable_option_name = disable_option_name
        self.disabled = disabled
        self.disabled_signature = disabled_signature
        self.configuration = configuration

    def _should_include(self, compile_state: Any) -> bool:
        # SQLAlchemy compiles a copy of a SELECT nested in the statement, which refers to the SELECT it copies
        # (or the first of its clones) through its clone origin. A SELECT that disables filtering itself, such as a
        # column_property()'s of a class loaded through a relationship, is left out wherever it is compiled.
        statement = compile_state.select_statement
        while statement is not None:
            if (
                id(statement) in self.disabled
                or getattr(statement, FILTERED_MARK, False)
                or statement._execution_options.get(self.disable_option_name)
            ):
                return False
            statement = getattr(statement, "_is_clone_of", None)
        return bool(super()._should_include(compile_state))

    def __reduce__(self) -> Any:
        # LoaderCriteriaOption's own rebuilds a plain LoaderCriteriaOption. The SELECTs of ``disabled`` belong to
        # the statement that loaded the instance.
        entity = self.entity.class_ if self.entity else self.root_entity
        if self.configuration is not None:
            # The condition of a class mapped to a SELECT is on its mapped attributes, which refer to its mapper:
            # it is made again, by a policy of the same configuration.
            return (
                _unpickle_mapped_subquery_criteria,
                (entity, self.include_aliases, self.configuration),
            )
        return (
            _unpickle_soft_delete_criteria,
            (entity, self._where_crit_orig, self.include_aliases, self.disable_option_name),
        )


_unpickling_policies: dict[Any, SoftDeletePolicy] = {}


def _unpickle_mapped_subquery_criteria(
    entity: Any, include_aliases: bool, configuration: Configuration
) -> SoftDeleteCriteria:
    policy = _unpickling_policies.get(configuration)
    if policy is None:
        field_name, option_name, ignored = configuration
        policy = SoftDeletePolicy(field_name, option_name, [IgnoredTable(name, schema) for name, schema in ignored])
        policy = _unpickling_policies.setdefault(configuration, policy)
    return SoftDeleteCriteria(
        entity,
        policy.mapped_subquery_condition(entity),
        include_aliases=include_aliases,
        disable_option_name=configuration[1],
        configuration=configuration,
    )


def _unpickle_soft_delete_criteria(
    entity: Any, where_criteria: Any, include_aliases: bool, disable_option_name: str
) -> SoftDeleteCriteria:
    return SoftDeleteCriteria(
        entity, where_criteria, include_aliases=include_aliases, disable_option_name=disable_option_name
    )


# Pickle stores these functions by module and name: pickled instances find them in the package's module, which
# re-exports them.
_unpickle_mapped_subquery_criteria.__module__ = PICKLE_MODULE
_unpickle_soft_delete_criteria.__module__ = PICKLE_MODULE


def filtered_copy(
    rewriter: Rewriter, from_obj: FromClause, memo: dict[int, FromClause | None], context: RewriteContext
) -> FromClause | None:
    """A copy of a subquery (or of an alias of one) around its rewritten statement, or None when the
    rewrite leaves the statement as it is. ``memo`` keeps the copies made for one statement."""
    key = id(from_obj)
    if key not in memo:
        element: Any = cast(AliasedReturnsRows, from_obj).element
        inner: Any
        if isinstance(from_obj, CTE) and from_obj._cte_alias is not None:
            # An alias of a CTE (cte.alias()): an alias of the CTE's filtered copy, which the WITH clause
            # renders.
            aliased_copy = filtered_copy(rewriter, from_obj._cte_alias, memo, context)
            if aliased_copy is None:
                memo[key] = None
                return None
            copy = from_obj._clone()
            copy._is_clone_of = from_obj
            copy._cte_alias = cast(CTE, aliased_copy)
            copy.element = cast(CTE, aliased_copy).element
            copy._reset_column_collection()
            memo[key] = copy
            return copy
        if isinstance(from_obj, CTE):
            # The statement of a recursive CTE refers to the CTE itself (the CTE it restates).
            restated = {id(from_obj)}
            cte: Any = from_obj
            while cte._restates is not None:
                cte = cte._restates
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
        filtered = None if unchanged else copy_selectable(from_obj, inner)
        if isinstance(filtered, CTE) and filtered._restates is None:
            # A CTE renders once, by name: where the statement still refers to the original (in a statement
            # that includes soft-deleted rows, through an alias in a nested SELECT), SQLAlchemy renders the
            # copy, which restates it.
            filtered._restates = cast(CTE, from_obj)
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
        # The new statement is this function's own copy: setting its attributes modifies nothing else.
        rewritten = stmt._generate()
        rewritten._raw_columns = list(self._place_in_all(stmt._raw_columns))
        rewritten._where_criteria = self._place_in_all(stmt._where_criteria)
        rewritten._having_criteria = self._place_in_all(stmt._having_criteria)
        rewritten._order_by_clauses = self._place_in_all(stmt._order_by_clauses)
        rewritten._group_by_clauses = self._place_in_all(stmt._group_by_clauses)
        rewritten._from_obj = tuple(self._swap_from(from_obj) for from_obj in stmt._from_obj)
        rewritten._setup_joins = self._swap_joins(stmt._setup_joins)
        if stmt._memoized_select_entities:
            # with_only_columns() sets the statement's earlier joins aside.
            memoized = []
            for entities in stmt._memoized_select_entities:
                joins = self._swap_joins(entities._setup_joins)
                if joins is not entities._setup_joins:
                    entities = entities._clone()
                    entities._setup_joins = joins
                memoized.append(entities)
            rewritten._memoized_select_entities = tuple(memoized)
        unplaced = tuple(copy for key, copy in self.copies.items() if key not in self.placed)
        if unplaced:
            rewritten._from_obj = rewritten._from_obj + unplaced
        return rewritten

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
            new_target, new_onclause = (target, onclause) if copy is None else (entity_over(entity, copy), target)
        elif isinstance(onclause, QueryableAttribute):
            # join(entity, Parent.children): to the given entity
            copy = self._copy_of(target)
            entity = annotated_entity(target) or relationship_entity(onclause)
            new_target, new_onclause = (target, onclause) if copy is None else (entity_over(entity, copy), onclause)
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


def rewrite_nested_selects(
    rewriter: Rewriter,
    stmt: Select[Any],
    memo: dict[int, Any],
    key: Any,
    many_froms: bool,
    context: RewriteContext,
) -> Any:
    """``stmt`` with the SELECTs nested in its WHERE and HAVING clauses and in the ON clauses of its joins
    rewritten like statements of their own (see ``SelectPlan.rewrites_nested``). One that disables soft-delete
    filtering is left as it is. Each one's plan is remembered by ``key`` (``stmt``'s) and its place.
    ``many_froms`` is whether ``stmt`` has more than one FROM element (see ``SelectPlan.many_froms``).

    A SELECT of HAVING that is also in the columns or GROUP BY is left, with what is nested in it, to the loader
    criteria: PostgreSQL matches HAVING to GROUP BY by expression, and SQLAlchemy 2.1.0 also applies the criteria to
    the correlated classes of a nested SELECT, so only the criteria render both the same.
    """
    froms = from_ids(stmt)
    if context.enclosing_froms:
        # Nested itself: the FROM elements it correlates to are not its own, for the SELECTs nested in it.
        froms = froms - correlated_froms(stmt, context.enclosing_froms, many_froms)
    nested = context.nested_in(froms, memo)
    place = itertools.count()

    def replace(element: Any, **kw: Any) -> Any:
        if not isinstance(element, (Select, CompoundSelect)):
            return None
        keyed = nested.keyed((key, "nested", next(place)) if key is not None else None)
        if isinstance(element, CompoundSelect):
            return rewriter.rewrite_compound_select(element, keyed)
        return rewriter.rewrite_select(element, keyed)

    new = stmt._generate()
    new._where_criteria = tuple(replace_selects(clause, replace) for clause in stmt._where_criteria)
    if stmt._having_criteria:
        listed = listed_selects(stmt)

        def replace_unlisted(element: Any, **kw: Any) -> Any:
            # One that is also in the columns or GROUP BY is left, with what is in it, to the criteria.
            return element if is_listed(element, listed) else replace(element)

        new._having_criteria = tuple(replace_selects(clause, replace_unlisted) for clause in stmt._having_criteria)
    joins = []
    for target, onclause, from_, flags in stmt._setup_joins:
        if (
            onclause is not None
            and not isinstance(onclause, QueryableAttribute)
            and any(True for _ in top_selects(onclause))
        ):
            onclause = replace_selects(onclause, replace)
        joins.append((target, onclause, from_, flags))
    new._setup_joins = tuple(joins)
    return new


def with_secondary_criteria(stmt: Select[Any], criteria: tuple[tuple[int, Any], ...]) -> Any:
    """``stmt`` with the soft-delete condition of each secondary table in ``criteria`` added to its join along
    the relationship (``relationship.and_()``). Joins are numbered as ``joins_of()`` lists them: the
    statement's own, then those ``with_only_columns()`` set aside."""
    conditions = dict(criteria)

    def add(stmt_joins: tuple[Any, ...], offset: int) -> tuple[Any, ...]:
        rewritten = []
        for position, (target, onclause, from_, flags) in enumerate(stmt_joins, start=offset):
            condition = conditions.get(position)
            if condition is not None:
                if isinstance(onclause, QueryableAttribute):
                    onclause = onclause.and_(condition)
                else:
                    target = cast(QueryableAttribute[Any], target).and_(condition)
            rewritten.append((target, onclause, from_, flags))
        return tuple(rewritten)

    rewritten = stmt._generate()
    rewritten._setup_joins = add(stmt._setup_joins, 0)
    offset = len(stmt._setup_joins)
    if stmt._memoized_select_entities:
        memoized = []
        for entities in stmt._memoized_select_entities:
            joins = add(entities._setup_joins, offset)
            offset += len(entities._setup_joins)
            if any(position in conditions for position in range(offset - len(joins), offset)):
                entities = entities._clone()
                entities._setup_joins = joins
            memoized.append(entities)
        rewritten._memoized_select_entities = tuple(memoized)
    return rewritten


def with_outer_join_filters(
    stmt: Select[Any], outer_joins: tuple[tuple[Any, Any], ...], filters: list[Any], copies: dict[int, FromClause]
) -> tuple[Any, list[Any]]:
    """``stmt`` with the soft-delete columns of ``filters`` that belong to the right side of one of its
    ``outer_joins`` filtered in that join's ON clause, and the columns left for the WHERE clause.

    The right sides are read from ``stmt`` itself, whose aliases of tables are its own. The outer joins nested in
    a join come first, so that each column goes to the innermost one. A join that gives no ON clause gets the
    one SQLAlchemy found, on the copies of the subqueries it refers to (``copies``).
    """
    remaining = {id(column): column for column in filters}

    def take(selectable: Any) -> list[Any]:
        leaves = {id(plain(leaf)) for leaf in join_leaves(selectable)}
        moved = [column for column in remaining.values() if id(plain(column.table)) in leaves]
        for column in moved:
            del remaining[id(column)]
        return [column.is_(None) for column in moved]

    from_objs = list(stmt._from_obj)
    all_joins = list(joins_of(stmt))
    for position, computed in outer_joins:
        if isinstance(position, tuple):
            kind, index, path = position
            tree = from_objs[index] if kind == "from" else all_joins[index][0]
            join = _join_at(tree, path)
            conditions = take(join.right)
            if conditions:
                tree = _with_on_conditions(tree, join, conditions)
                if kind == "from":
                    from_objs[index] = tree
                else:
                    all_joins[index] = (tree, *all_joins[index][1:])
        else:
            conditions = take(join_target_selectable(all_joins[position][0]))
            if conditions:
                computed = on_copies(computed, copies) if computed is not None else None
                all_joins[position] = _join_with_conditions(all_joins[position], conditions, computed)

    rewritten = stmt._generate()
    rewritten._from_obj = tuple(from_objs)
    offset = len(stmt._setup_joins)
    rewritten._setup_joins = tuple(all_joins[:offset])
    if stmt._memoized_select_entities:
        memoized = []
        for entities in stmt._memoized_select_entities:
            joins_set_aside = tuple(all_joins[offset : offset + len(entities._setup_joins)])
            offset += len(entities._setup_joins)
            if any(new is not old for new, old in zip(joins_set_aside, entities._setup_joins)):
                entities = entities._clone()
                entities._setup_joins = joins_set_aside
            memoized.append(entities)
        rewritten._memoized_select_entities = tuple(memoized)
    return rewritten, list(remaining.values())


def _join_at(from_obj: Any, path: tuple[str, ...]) -> Join:
    for step in path:
        from_obj = ungrouped(from_obj)
        from_obj = from_obj.left if step == "l" else from_obj.right
    return cast(Join, ungrouped(from_obj))


def _join_with_conditions(join: tuple[Any, ...], conditions: list[Any], computed: Any) -> tuple[Any, ...]:
    """A join of a statement (a ``joins()`` entry) with ``conditions`` added to its ON clause: to the
    relationship it joins along, or to its ON clause (``computed``, for a join that gives none)."""
    target, onclause, from_, flags = join
    if isinstance(target, QueryableAttribute) and onclause is None:
        return (target.and_(*conditions), onclause, from_, flags)
    if isinstance(onclause, QueryableAttribute):
        return (target, onclause.and_(*conditions), from_, flags)
    return (target, and_(cast(Any, onclause if onclause is not None else computed), *conditions), from_, flags)


def _with_on_conditions(from_obj: Any, join: Join, conditions: list[Any]) -> Any:
    """``from_obj``, a join tree written with Core, with ``conditions`` added to the ON clause of ``join``."""
    replacement = join._clone()
    replacement.onclause = and_(cast(Any, join.onclause), *conditions)

    def replace(element: Any, **kw: Any) -> Any:
        return replacement if element is join else None

    return visitors.replacement_traverse(from_obj, {}, replace)


def apply_plan(
    rewriter: Rewriter,
    stmt: Select[Any],
    plan: SelectPlan,
    analysis: FromAnalysis | None,
    key: Any,
    context: RewriteContext,
) -> Any:
    """Return a copy of ``stmt`` with ``plan`` applied.

    ``analysis`` holds what the analysis of the FROM clause found when it was just analyzed;
    otherwise the subqueries are found again by reading the statement.

    In order: filtered copies of the FROM clause's subqueries, the nested SELECTs rewritten, the loader options of
    joined eager loads through soft-deletable secondaries, the loader criteria, the conditions of the secondaries
    of relationship joins, then the soft-delete columns: in the ON clause of a LEFT OUTER JOIN they are on the right
    of, else in the WHERE clause.
    """
    policy = rewriter.policy
    if analysis is None and (plan.rewrites_subqueries or plan.filters_table_aliases):
        analysis = _read_analysis(rewriter, stmt, context)
    copies: dict[int, FromClause] = {}
    memo: dict[int, FromClause | None] = context.copy_memo if context.copy_memo is not None else {}
    if plan.rewrites_subqueries and analysis is not None:
        for from_obj in analysis.subqueries:
            copy = filtered_copy(rewriter, from_obj, memo, context)
            if copy is not None:
                copies[id(from_obj)] = copy
        if copies:
            stmt = place_copies(stmt, copies, analysis.join_lefts)

    if plan.rewrites_nested:
        stmt = rewrite_nested_selects(rewriter, stmt, memo, key, plan.many_froms, context)

    secondary_options = plan.secondary_options
    if secondary_options is None:
        secondary_options = tuple(joined_secondary_options(policy, stmt)[0])
    if secondary_options:
        stmt = stmt.options(*secondary_options)

    stmt = with_criteria(policy, stmt, dict(plan.criteria_entities), plan.holds_selects)

    if plan.secondary_criteria:
        stmt = with_secondary_criteria(stmt, plan.secondary_criteria)

    filters: list[Any] = list(plan.filter_columns)
    if plan.filters_table_aliases and analysis is not None and not context.relationship_load:
        for alias in analysis.table_aliases:
            column = policy.alias_column(alias)
            if column is not None:
                filters.append(column)

    if plan.outer_joins:
        stmt, filters = with_outer_join_filters(stmt, plan.outer_joins, filters, copies)

    for column in filters:
        stmt = stmt.filter(column.is_(None))
    return stmt


def _read_analysis(rewriter: Rewriter, stmt: Select[Any], context: RewriteContext) -> FromAnalysis:
    """What the analysis of the FROM clause found, read again from a statement whose plan was remembered."""
    scan = FromScan(stmt)
    aliases = [alias for alias in scan.table_aliases.values() if rewriter.policy.alias_column(alias) is not None]
    if context.relationship_load:
        aliases = []
    return FromAnalysis(list(scan.subqueries.values()), {}, aliases)
