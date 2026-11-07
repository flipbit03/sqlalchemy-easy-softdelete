"""Reading a statement: its FROM clause, the classes eager loading and the mapping load, soft-deletable
secondaries, outer joins, and nested SELECTs."""

from __future__ import annotations

import itertools
from typing import Any

from sqlalchemy import Column, Table, inspect
from sqlalchemy.orm import Mapper, RelationshipProperty
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.orm.strategy_options import Load
from sqlalchemy.orm.util import AliasedInsp
from sqlalchemy.sql import Alias, CompoundSelect, Join, Select, TableClause, visitors
from sqlalchemy.sql import util as sql_util
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.selectable import Exists, FromClause, FromGrouping, ScalarSelect, SelectBase, Values

from sqlalchemy_easy_softdelete.handler.rewriter.clauses import (
    aliased_table,
    annotated_entity,
    column_property_selects,
    final_froms_without_loader_options,
    in_polymorphic_alias,
    join_leaves,
    join_target_selectable,
    joins_of,
    plain,
    relationship_target,
    same_expression,
    selects_from_mapper,
    subquery_in,
    top_selects,
    ungrouped,
)
from sqlalchemy_easy_softdelete.handler.rewriter.plan import Criteria, Reason, RewriteContext
from sqlalchemy_easy_softdelete.handler.rewriter.policy import SoftDeletePolicy, entity_class_of


def nested_selects(stmt: Select[Any]) -> Any:
    """The SELECTs nested in the WHERE and HAVING clauses of ``stmt`` and in the ON clauses of its joins (see
    ``SelectPlan.rewrites_nested``), but not those of HAVING that are also in its columns, ORDER BY or GROUP BY
    clauses (see ``listed_selects``)."""
    clauses: list[Any] = [*stmt._where_criteria]
    if stmt._having_criteria:
        listed = listed_selects(stmt)
        for clause in stmt._having_criteria:
            yield from (select for select in top_selects(clause) if not is_listed(select, listed))
    clauses += [
        onclause
        for _target, onclause, _from, _flags in stmt._setup_joins
        if onclause is not None and not isinstance(onclause, QueryableAttribute)
    ]
    for clause in clauses:
        yield from top_selects(clause)


def listed_selects(stmt: Select[Any]) -> list[Any]:
    """The SELECTs nested in the columns, ORDER BY and GROUP BY clauses of ``stmt``, which loader criteria filter
    (see ``SelectPlan.rewrites_nested``)."""
    # A column that is a FROM element is an entity; its SELECT is its own FROM clause.
    clauses = [
        *(column for column in stmt._raw_columns if not isinstance(column, FromClause)),
        *stmt._order_by_clauses,
        *stmt._group_by_clauses,
    ]
    return [select for clause in clauses for select in top_selects(clause)]


def is_listed(element: Any, listed: list[Any]) -> bool:
    """Whether ``element``, a SELECT of a HAVING clause, is the same expression as one of ``listed``: PostgreSQL
    matches HAVING to GROUP BY by expression, so they must render the same."""
    if not isinstance(element, (Select, CompoundSelect)):
        return False
    return any(same_expression(element, select) for select in listed)


def holds_selects(stmt: Select[Any]) -> bool:
    """Whether ``stmt`` holds SELECTs anywhere (see ``SelectPlan.holds_selects``)."""
    return any(
        element is not stmt and isinstance(element, (Select, CompoundSelect)) for element in visitors.iterate(stmt)
    )


def mappers_in(element: Any) -> list[Mapper[Any]]:
    """The mappers whose tables ``element`` selects from, through their entities or columns."""
    found: dict[Mapper[Any], None] = {}
    for child in visitors.iterate(element):
        mapper = getattr(child, "_annotations", {}).get("parentmapper")
        if isinstance(mapper, Mapper) and selects_from_mapper(child, mapper):
            found.setdefault(mapper, None)
    return list(found)


def from_ids(stmt: Select[Any]) -> frozenset[int]:
    """The ids of the FROM elements of ``stmt`` that a SELECT nested in it may correlate to: those of its FROM list,
    its joins and its columns and criteria, and the tables and subqueries of the joins among them."""
    found: set[int] = set()

    def add(from_obj: Any) -> None:
        if from_obj is None:
            return
        for leaf in join_leaves(ungrouped(from_obj)):
            # A filtered copy stands for what it is a clone of, or restates (a CTE): the nested SELECT may still
            # refer to that.
            stack = [plain(leaf)]
            while stack:
                element = stack.pop()
                if element is None or id(element) in found:
                    continue
                found.add(id(element))
                stack.append(getattr(element, "_is_clone_of", None))
                stack.append(getattr(element, "_restates", None))

    for from_obj in stmt._from_obj:
        add(from_obj)
    for target, _onclause, from_, _flags in joins_of(stmt):
        add(join_target_selectable(target) if not isinstance(target, FromClause) else target)
        add(from_)
    for element in (*stmt._raw_columns, *stmt._where_criteria):
        for from_obj in element._from_objects:
            add(from_obj)
    return frozenset(found)


def froms_without_criteria(stmt: Select[Any]) -> list[Any]:
    """The FROM elements of ``stmt``: those SQLAlchemy finds for it without its WHERE and HAVING criteria, and
    those of the criteria that are not among them."""
    bare = stmt._generate()
    bare._where_criteria = ()
    bare._having_criteria = ()
    froms = list(final_froms_without_loader_options(bare))
    seen = {id(plain(leaf)) for from_obj in froms for leaf in join_leaves(ungrouped(from_obj))}
    for criterion in (*stmt._where_criteria, *stmt._having_criteria):
        for from_obj in criterion._from_objects:
            if id(plain(from_obj)) not in seen:
                seen.add(id(plain(from_obj)))
                froms.append(plain(from_obj))
    return froms


def correlated_froms(stmt: Select[Any], enclosing: frozenset[int], many_froms: bool) -> set[int]:
    """The ids of the FROM elements of the enclosing statement that ``stmt``, a nested SELECT, correlates to, as
    SQLAlchemy does: those of ``correlate()``, all but those of ``correlate_except()``, or else all of them, when
    ``stmt`` has more than one FROM element (``many_froms``; one that has only one selects from it again)."""
    correlate, correlate_except, auto_correlate = (stmt._correlate, stmt._correlate_except, stmt._auto_correlate)
    if correlate:
        return {id(plain(from_obj)) for from_obj in correlate} & enclosing
    if correlate_except is not None:
        return set(enclosing) - {id(plain(from_obj)) for from_obj in correlate_except}
    return set(enclosing) if auto_correlate and many_froms else set()


def collect_from(
    policy: SoftDeletePolicy,
    from_obj: Any,
    columns: list[Column[Any]],
    subqueries: list[FromClause],
    aliases: list[Alias],
) -> None:
    """Collect the soft-delete columns, subqueries and aliases of tables of one element of a statement's
    FROM list."""
    if isinstance(from_obj, Table):
        policy.collect_table(from_obj, columns)
        return

    if isinstance(from_obj, Join):
        # Join (and the ORM's _ORMJoin) holds two things: 'left' and 'right'. Check both.
        _collect_join(policy, from_obj, columns, subqueries, aliases)
        return

    if isinstance(from_obj, FromGrouping):
        # A parenthesized FROM element, such as a JOIN nested on the right of another JOIN.
        collect_from(policy, from_obj.element, columns, subqueries, aliases)
        return

    if subquery_in(from_obj) is not None:
        # A subquery, or an alias of one (an alias of an aliased() entity).
        subqueries.append(from_obj)
        return

    if isinstance(from_obj, TableClause) or isinstance(from_obj, TextClause):
        # TableClause/TextClause objects are raw text SQL identifiers and as such, we cannot
        # introspect or do anything about this statement.
        return

    if isinstance(from_obj, Values):
        # A VALUES (...) construct is a literal row set with no underlying table,
        # so there is nothing to apply a soft-delete filter to. Leave it untouched
        # (same as raw-text identifiers above).
        return

    if aliased_table(from_obj) is not None:
        # An alias of a table (aliased(Model)): filtered on its own soft-delete column.
        aliases.append(from_obj)
        return

    if isinstance(from_obj, Alias):
        raise NotImplementedError(f'Unsupported object "{(type(from_obj.element))}" inside Alias in statement.froms')

    raise NotImplementedError(f'Unsupported object "{(type(from_obj))}" in statement.froms')


def _collect_join(
    policy: SoftDeletePolicy,
    join: Join,
    columns: list[Column[Any]],
    subqueries: list[FromClause],
    aliases: list[Alias],
) -> None:
    """Handle multiple, and potentially recursive joins: nested joins, then subqueries, then tables and
    aliases of tables."""
    sides = (ungrouped(join.left), ungrouped(join.right))
    for side in sides:
        if isinstance(side, Join):
            _collect_join(policy, side, columns, subqueries, aliases)
    for side in sides:
        # A subquery, or an alias of one (an alias of an aliased() entity).
        if not isinstance(side, Join) and subquery_in(side) is not None:
            subqueries.append(side)
    for side in sides:
        if isinstance(side, Table):
            policy.collect_table(side, columns)
        elif aliased_table(side) is not None:
            aliases.append(side)


class FromScan:
    """The subqueries (and aliases of subqueries) a statement puts in its FROM clause, found by reading
    the statement, without compiling it.

    These are the subqueries of ``select_from()``, of joins (target, left side, the target of a
    relationship), and those the statement's columns and WHERE criteria select from, as such or
    through an ``aliased()`` entity or a mapped class. A plan is only reused when, for the statement
    it was made from, this finds the subqueries that the analysis of its FROM clause found.
    ``explicit`` holds those the statement names as a FROM: in ``select_from()``, as the target of a
    join, or as the left side of a join that does not go along a relationship.
    """

    def __init__(self, stmt: Select[Any]) -> None:
        self.subqueries: dict[int, FromClause] = {}
        self.explicit: set[int] = set()
        self.table_aliases: dict[int, Alias] = {}
        for target, onclause, from_, _flags in joins_of(stmt):
            along_relationship = isinstance(target, QueryableAttribute) or isinstance(onclause, QueryableAttribute)
            self._visit(relationship_target(target) if isinstance(target, QueryableAttribute) else target, True)
            self._visit(from_, not along_relationship)
            for attribute in (target, onclause):
                if isinstance(attribute, QueryableAttribute):
                    # A join along a relationship goes from the attribute's own entity.
                    self._visit(attribute.parent.selectable, False)
        for from_obj in stmt._from_obj:
            self._visit(from_obj, True)
        columns = itertools.chain(
            stmt._raw_columns,
            stmt._where_criteria,
            # with_only_columns() sets the statement's earlier entities aside, along with their joins.
            *(entities._raw_columns for entities in stmt._memoized_select_entities),
        )
        for from_obj in itertools.chain.from_iterable(element._from_objects for element in columns):
            self._visit(from_obj, False)

    def _visit(self, from_obj: Any, explicit: bool) -> None:
        if from_obj is None:
            return
        from_obj = plain(ungrouped(from_obj))
        if isinstance(from_obj, Join):
            self._visit(from_obj.left, explicit)
            self._visit(from_obj.right, explicit)
        elif subquery_in(from_obj) is not None:
            self.subqueries.setdefault(id(from_obj), from_obj)
            if explicit:
                self.explicit.add(id(from_obj))
        elif aliased_table(from_obj) is not None:
            self.table_aliases.setdefault(id(from_obj), from_obj)


def implicit_join_lefts(stmt: Select[Any], final_froms: Any) -> dict[int, Any]:
    """The left side SQLAlchemy chose in ``final_froms`` (the analyzed FROM clause of ``stmt``) for each
    join of ``stmt`` that does not give one (``join(Target)``, ``join(Target, onclause)``), by the id of
    the join: the FROM element the join attaches to, as SQLAlchemy finds it among those of its left."""
    by_right = joins_by_right(final_froms)
    lefts: dict[int, Any] = {}
    for join in joins_of(stmt):
        target, onclause, from_, _flags = join
        if from_ is not None or isinstance(target, QueryableAttribute) or isinstance(onclause, QueryableAttribute):
            continue
        joined = by_right.get(id(plain(ungrouped(target))))
        if joined is None:
            continue
        candidates = join_leaves(joined.left)
        indexes = sql_util.find_left_clause_to_join_from(candidates, joined.right, joined.onclause)
        if len(indexes) == 1:
            lefts[id(join)] = candidates[indexes[0]]
    return lefts


def joins_by_right(final_froms: Any) -> dict[int, Join]:
    """The joins of ``final_froms`` (the analyzed FROM clause of a statement) by the id of their right side."""
    found: dict[int, Join] = {}
    stack = list(final_froms)
    while stack:
        from_obj = ungrouped(stack.pop())
        if isinstance(from_obj, Join):
            found.setdefault(id(plain(ungrouped(from_obj.right))), from_obj)
            stack.extend((from_obj.left, from_obj.right))
    return found


def joins_to_without_its_columns(stmt: Select[Any], targets: set[int]) -> bool:
    """Whether the first join of ``stmt``, with no ``select_from()``, goes to one of ``targets`` (by id) without a
    left side of its own, with an ON clause that names no table but the target (``ON true``, ``ON target.x > 0``):
    SQLAlchemy then chooses the left side among the FROMs of the statement's columns, which still include the
    original of the copy. A later join attaches to the joins before it, and an ON clause naming another table
    tells SQLAlchemy the side."""
    if stmt._from_obj:
        return False
    all_joins = list(
        itertools.chain(*(entities._setup_joins for entities in stmt._memoized_select_entities), stmt._setup_joins)
    )
    if not all_joins:
        return False
    target, onclause, from_, _flags = all_joins[0]
    if (
        from_ is not None
        or onclause is None
        or isinstance(target, QueryableAttribute)
        or isinstance(onclause, QueryableAttribute)
    ):
        return False
    plain_target = plain(ungrouped(target))
    if id(plain_target) not in targets:
        return False
    return all(plain(table) is plain_target for table in _tables_named_in(onclause))


def _tables_named_in(clause: Any) -> list[Any]:
    """The tables (and other FROM elements) whose columns ``clause`` names, outside the SELECTs nested in it
    (an EXISTS names the tables of its own FROM clause, which SQLAlchemy does not join to)."""
    tables: list[Any] = []
    stack = [clause]
    while stack:
        element = stack.pop()
        if isinstance(element, (SelectBase, ScalarSelect, Exists)):
            continue
        table = getattr(element, "table", None)
        if table is not None:
            tables.append(table)
        stack.extend(element.get_children())
    return tables


def outer_joins(
    stmt: Select[Any], final_froms: Any, columns: list[Column[Any]], table_aliases: list[Alias]
) -> tuple[tuple[Any, Any], ...]:
    """See ``SelectPlan.outer_joins``."""
    filtered = {id(column.table) for column in columns} | {id(plain(alias)) for alias in table_aliases}
    if not filtered:
        return ()

    def holds_filtered(selectable: Any) -> bool:
        return any(id(plain(leaf)) in filtered for leaf in join_leaves(selectable))

    found: list[tuple[Any, Any]] = []
    for index, from_obj in enumerate(stmt._from_obj):
        for path, join in outer_joins_in(from_obj):
            if holds_filtered(join.right):
                found.append((("from", index, path), None))
    by_right: dict[int, Join] | None = None
    for position, (target, onclause, _from, flags) in enumerate(joins_of(stmt)):
        # A join to a join written with Core: its own outer joins come first.
        for path, join in outer_joins_in(target):
            if holds_filtered(join.right):
                found.append((("target", position, path), None))
        if not flags.get("isouter") or flags.get("full"):
            continue
        selectable = join_target_selectable(target)
        if selectable is None or not holds_filtered(selectable):
            continue
        computed = None
        if onclause is None and not isinstance(target, QueryableAttribute):
            if by_right is None:
                by_right = joins_by_right(final_froms)
            joined = by_right.get(id(plain(ungrouped(selectable))))
            if joined is None or joined.onclause is None:
                continue
            computed = joined.onclause
        found.append((position, computed))
    return tuple(found)


def outer_joins_in(from_obj: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Join]]:
    """The LEFT OUTER JOINs of a join tree written with Core (or ``orm.join()``), each with its path from the
    root (``l``/``r`` steps)."""
    from_obj = ungrouped(from_obj)
    if not isinstance(from_obj, Join):
        return []
    found = outer_joins_in(from_obj.left, path + ("l",)) + outer_joins_in(from_obj.right, path + ("r",))
    if from_obj.isouter and not from_obj.full:
        found.append((path, from_obj))
    return found


def refers_to_statement_objects(onclause: Any) -> bool:
    """Whether ``onclause`` refers to a FROM other than a table (an alias, a subquery): one of its statement's."""
    if onclause is None:
        return False
    return any(not isinstance(from_obj, Table) for from_obj in onclause._from_objects)


def loaded_mappers(stmt: Select[Any]) -> list[Mapper[Any]]:
    """The mappers of the entities ``stmt`` selects (classes and ``aliased()`` entities)."""
    return [
        insp.mapper
        for insp in (annotated_entity(column) for column in stmt._raw_columns if isinstance(column, FromClause))
        if isinstance(insp, (Mapper, AliasedInsp))
    ]


def eager_loaded_entities(
    policy: SoftDeletePolicy, stmt: Select[Any]
) -> tuple[list[type], list[Reason], list[AliasedInsp[Any]]]:
    """Soft-deletable entities named in the loader options' paths (joinedload, subqueryload, etc.), why the list
    does not hold for every statement of the same shape, if it does not, and the ``aliased()`` targets of the
    relationships of the classes the statement loads (see ``mapping_reach``).

    These are filtered with ``with_loader_criteria``, so the soft-delete filter also reaches the
    JOINed aliases and the separate queries that eager loading creates. Column loader options
    (``load_only``, ``defer``) name their entity too; their criteria repeat a filter the statement
    already has, which changes nothing.

    A subclass in an inheritance hierarchy (a relationship loaded ``of_type()`` a subclass) filters the hierarchy's
    base class too: SQLAlchemy applies criteria by the base class, which reach its subclasses.

    A class mapped to a SELECT that leaves out the soft-delete column is filtered through its primary key
    (see ``SoftDeletePolicy.mapped_subquery_condition``) where it is joined eager loaded.

    The targets of relationships that are eager by their mapping (``lazy="joined"``), of the classes the statement
    loads or its loader options reach, are filtered too: SQLAlchemy joins them with no loader option.

    An ``aliased()`` entity over a subquery whose statement includes soft-deleted rows is left out:
    eager loading into it loads its rows as they are. That depends on the execution options of the
    statement inside the subquery, which cache keys leave out.
    """
    entities: dict[type, None] = {}
    reasons: list[Reason] = []
    # The mappers the statement loads, as entities or along its loader options: their relationships that are
    # eager by their mapping (lazy="joined") join their targets too.
    loaded = loaded_mappers(stmt)
    _add_loader_option_entities(policy, stmt, loaded, entities, reasons)
    joined_targets, mapped_aliases = mapping_reach(loaded)
    _add_joined_targets(policy, joined_targets, entities)
    _add_listed_select_entities(policy, stmt, loaded + [entity.mapper for entity in joined_targets], entities)
    return list(entities), reasons, mapped_aliases


def _add_loader_option_entities(
    policy: SoftDeletePolicy,
    stmt: Select[Any],
    loaded: list[Mapper[Any]],
    entities: dict[type, None],
    reasons: list[Reason],
) -> None:
    """The entities of the paths of the statement's loader options, into ``entities``; the mappers they load,
    into ``loaded``."""
    for opt in stmt._with_options:
        if not isinstance(opt, Load):
            continue

        # An option holds a loader for each path it loads, including those its options() add:
        # entities (mappers, aliased() entities) and the relationships between them.
        loaders = [(loader.path, getattr(loader, "strategy", None)) for loader in opt.context]
        for path, strategy in loaders or [(opt.path, None)]:
            # PathRegistry is iterable at runtime but not typed as such
            path_elements: list[Any] = list(path) if path is not None else []  # type: ignore[call-overload]
            joined = strategy is not None and ("lazy", "joined") in strategy
            _add_path_entities(policy, path_elements, joined, loaded, entities, reasons)


def _add_path_entities(
    policy: SoftDeletePolicy,
    path_elements: list[Any],
    joined: bool,
    loaded: list[Mapper[Any]],
    entities: dict[type, None],
    reasons: list[Reason],
) -> None:
    """The entities of one loader path, into ``entities``; the mappers it loads, into ``loaded``. ``joined`` is
    whether the path's loader is a joined eager load."""
    for position, path_element in enumerate(path_elements):
        if isinstance(path_element, (Mapper, AliasedInsp)):
            loaded.append(path_element.mapper)
        if isinstance(path_element, AliasedInsp):
            if position == 0:
                # An aliased() entity the option starts from is in the statement's FROM
                # clause, which the rewrite handles; the entities it loads are the ones to filter.
                continue
            subquery = subquery_in(path_element.selectable)
            if subquery is not None:
                reasons.append(Reason.SUBQUERY_IN_LOADER_PATH)
                if not policy.filtered_mappers_in(subquery.element)[1]:
                    continue
        entity_class = entity_class_of(path_element)
        if entity_class is None:
            continue
        if policy.is_soft_deletable_entity(entity_class) or (
            # Only a joined eager load into it, which the path of its loader ends at: elsewhere it is in
            # a FROM clause, as a filtered copy of its SELECT.
            joined and position == len(path_elements) - 1 and policy.mapped_subquery_condition(entity_class) is not None
        ):
            entities.setdefault(entity_class, None)
        mapper = getattr(path_element, "mapper", None)
        if position > 0 and isinstance(mapper, Mapper) and mapper.inherits is not None:
            # SQLAlchemy applies loader criteria by the base class of an inheritance hierarchy, which
            # reach its subclasses, for a relationship loaded as a subclass (of_type()): it leaves out
            # those of the subclass.
            base = mapper.base_mapper.class_
            if policy.is_soft_deletable_entity(base):
                entities.setdefault(base, None)


def _add_joined_targets(policy: SoftDeletePolicy, joined_targets: list[Any], entities: dict[type, None]) -> None:
    """The targets of the relationships the mapping joins (``lazy="joined"``), into ``entities``."""
    for entity in joined_targets:
        if isinstance(entity, AliasedInsp) and not policy.filters_alias(entity):
            # SQLAlchemy applies the class's criteria to its own join of such an alias.
            continue
        target = entity.mapper.class_
        if policy.is_soft_deletable_entity(target) or policy.mapped_subquery_condition(target) is not None:
            entities.setdefault(target, None)


def _add_listed_select_entities(
    policy: SoftDeletePolicy, stmt: Select[Any], mappers: list[Mapper[Any]], entities: dict[type, None]
) -> None:
    """The classes of the SELECTs in the statement's columns, ORDER BY and GROUP BY clauses, and in the
    ``column_property()`` expressions of ``mappers``, into ``entities``.

    Those SELECTs are filtered by criteria for the classes they select from: SQLAlchemy keys result rows by the
    statement's columns, which the rewrite keeps, and PostgreSQL matches ORDER BY and HAVING to them (or to GROUP
    BY) by expression, so a SELECT of HAVING that is also there is left to the same criteria (see
    ``rewrite_nested_selects``). One that disables filtering is left out by the criteria (see ``SoftDeleteCriteria``).
    """
    for select in listed_selects(stmt) + column_property_selects(mappers):
        for mapper in mappers_in(select):
            if policy.is_soft_deletable_entity(mapper.class_):
                entities.setdefault(mapper.class_, None)


def mapping_reach(mappers: list[Mapper[Any]]) -> tuple[list[Any], list[AliasedInsp[Any]]]:
    """What the mapping of ``mappers`` (the classes a statement loads) brings into its loads, through the
    relationships of those classes and of their subclasses (polymorphic loading), and of their targets in turn:

    - the targets (mappers, or ``aliased()`` entities) of the relationships that are eager by their mapping
      (``lazy="joined"``), which SQLAlchemy joins while it compiles the statement;
    - the ``aliased()`` targets of any of those relationships, which the statement's criteria reach in their
      loads, since criteria propagate to the loads of the instances a statement loads.
    """
    joined: dict[int, Any] = {}
    aliases: dict[int, AliasedInsp[Any]] = {}
    seen: set[tuple[Mapper[Any], bool]] = set()
    eager = [(mapper, True) for mapper in mappers]
    while eager:
        mapper, joins = eager.pop()
        for each in mapper.self_and_descendants:
            if (each, joins) in seen or (each, True) in seen:
                continue
            seen.add((each, joins))
            for prop in each.relationships:
                entity = prop.entity
                if isinstance(entity, AliasedInsp):
                    aliases.setdefault(id(entity), entity)
                by_join = joins and ("lazy", "joined") in prop.strategy_key
                if by_join:
                    joined.setdefault(id(entity), entity)
                eager.append((prop.mapper, by_join))
    return list(joined.values()), list(aliases.values())


def keep_criteria_off_unfilterable_aliases(
    policy: SoftDeletePolicy,
    stmt: Select[Any],
    entities: Criteria,
    context: RewriteContext,
    mapped_aliases: list[AliasedInsp[Any]] | None = None,
) -> list[Reason]:
    """Turn off, in ``entities``, the eager loading criteria's reach into the ``aliased()`` forms of
    each class that has, anywhere in ``stmt`` (its elements, the ``of_type()`` entities of its joins,
    the paths of its loader options), an ``aliased()`` entity over a subquery they must not filter: one whose
    statement includes soft-deleted rows, or one that does not pass on the soft-delete column (SQLAlchemy would
    then filter the class's table, which the statement does not select from there). Return why the result does
    not hold for every statement of the same shape, if it does not: when such entities are over subqueries, whose
    execution options cache keys leave out.

    ``mapped_aliases`` are the ``aliased()`` targets of the relationships of the classes the statement
    loads (see ``mapping_reach``), which the criteria reach in their loads too. They belong to the mapping,
    so they hold for every statement of the same shape.
    """
    reasons: list[Reason] = []
    seen: set[int] = set()
    mapping_owned = {id(insp) for insp in mapped_aliases or ()}
    path_entities, path_dependent = _loader_path_entities(stmt, context)
    if path_dependent:
        # It depends on the path of the relationship load, which cache keys leave out.
        reasons.append(Reason.LOAD_PATH_DEPENDENT)
    for insp in itertools.chain(
        (annotated_entity(element) for element in visitors.iterate(stmt)),
        _of_type_entities(stmt),
        path_entities,
        mapped_aliases or (),
    ):
        if not isinstance(insp, AliasedInsp) or id(insp) in seen:
            continue
        # The criteria of a class reach the aliased() entities of its subclasses too.
        classes = [mapper.class_ for mapper in insp.mapper.iterate_to_root() if mapper.class_ in entities]
        if not classes:
            continue
        seen.add(id(insp))
        subquery = subquery_in(insp.selectable)
        if subquery is None:
            continue
        if id(insp) not in mapping_owned:
            reasons.append(Reason.ALIASED_SUBQUERY_ENTITY)
        if not policy.filters_alias(insp):
            for entity_class in classes:
                entities[entity_class] = False
    return reasons


def _of_type_entities(stmt: Select[Any]) -> list[Any]:
    """The ``of_type()`` entities of the relationship joins of ``stmt``, which iterating the statement does not
    reach: ``join(Parent.children.of_type(alias))``."""
    found: list[Any] = []
    for target, onclause, _from, _flags in joins_of(stmt):
        for attribute in (target, onclause):
            if isinstance(attribute, QueryableAttribute):
                of_type = attribute._of_type
                if of_type is not None:
                    found.append(inspect(of_type))
    return found


def _loader_path_entities(stmt: Select[Any], context: RewriteContext) -> tuple[list[Any], bool]:
    """The ``aliased()`` entities in the paths of the loader options of ``stmt``
    (``selectinload(Parent.children.of_type(alias))``), which iterating the statement does not reach, and
    whether the result depends on the path of the relationship load being rewritten (see
    ``RewriteContext.load_path``): in a relationship load, only the paths on the load's own path count."""
    found: list[Any] = []
    own = list(context.load_path or ()) if context.relationship_load else None
    path_dependent = False
    for opt in stmt._with_options:
        if not isinstance(opt, Load):
            continue
        for path in [loader.path for loader in opt.context] or [opt.path]:
            path_elements: list[Any] = list(path) if path is not None else []  # type: ignore[call-overload]
            aliases = [element for element in path_elements if isinstance(element, AliasedInsp)]
            if not aliases:
                continue
            if own is not None:
                path_dependent = True
                shared = min(len(own), len(path_elements))
                if any(a is not b for a, b in zip(own[:shared], path_elements[:shared])):
                    continue
            found.extend(aliases)
    return found, path_dependent


_Path = list[Any]
"""A loader path: ``[entity, relationship, entity, ...]``."""


def secondary_criteria(policy: SoftDeletePolicy, stmt: Select[Any]) -> tuple[tuple[int, Any], ...]:
    """See ``SelectPlan.secondary_criteria``."""
    criteria: list[tuple[int, Any]] = []
    for index, (target, onclause, _from, _flags) in enumerate(joins_of(stmt)):
        attribute = onclause if isinstance(onclause, QueryableAttribute) else target
        if not isinstance(attribute, QueryableAttribute):
            continue
        prop = attribute.property
        if not isinstance(prop, RelationshipProperty):
            continue
        condition = policy.secondary_condition(prop)
        if condition is not None:
            criteria.append((index, condition))
    return tuple(criteria)


def joined_secondary_options(policy: SoftDeletePolicy, stmt: Select[Any]) -> tuple[list[Any], bool]:
    """See ``SelectPlan.secondary_options``: a loader option for each joined eager load of ``stmt`` along a
    relationship whose secondary is soft-deletable, which repeats it with the secondary's condition in
    ``relationship.and_()``, and whether the options depend on the statement's own ``aliased()`` entities.
    SQLAlchemy joins its own alias of the secondary for a joined eager load, which the statement does not
    hold; it adapts those criteria to it.

    The joined eager loads are those of the statement's ``joinedload()`` options, the last one for each path,
    and those of the relationships that are eager by their mapping (``lazy="joined"``) of the classes the
    statement loads and of those joined eager loading reaches, unless an option of the statement speaks for
    their path or the statement has a wildcard option. An option for the same path, with the same arguments,
    takes the place of the statement's.
    """
    explicit, named, wildcard = _explicit_joined_loads(stmt)
    options = _SecondaryOptions(policy, named, wildcard)
    for path, innerjoin, criteria in explicit.values():
        options.add(path, innerjoin, criteria)
    for column in stmt._raw_columns:
        insp = annotated_entity(column) if isinstance(column, FromClause) else None
        if isinstance(insp, (Mapper, AliasedInsp)):
            options.along_mapping([insp])
    for path, _innerjoin, _criteria in explicit.values():
        options.along_mapping(path)
    return options.options, options.statement_specific


def _explicit_joined_loads(
    stmt: Select[Any],
) -> tuple[dict[tuple[int, ...], tuple[_Path, Any, tuple[Any, ...]]], set[tuple[int, ...]], bool]:
    """The joined eager loads of the statement's options, the last one for each path, with each one's ``innerjoin``
    and criteria; the paths the options choose a strategy for; and whether an option is a relationship wildcard."""
    explicit: dict[tuple[int, ...], tuple[_Path, Any, tuple[Any, ...]]] = {}
    named: set[tuple[int, ...]] = set()
    wildcard = False
    for opt in stmt._with_options:
        if not isinstance(opt, Load):
            # A wildcard option (raiseload("*")): its path is a token.
            wildcard = wildcard or any(_relationship_token(element) for element in getattr(opt, "path", ()))
            continue
        for loader in opt.context:
            # PathRegistry is iterable at runtime but not typed as such
            path: _Path = list(loader.path)  # type: ignore[call-overload]
            if any(isinstance(element, str) for element in path):
                # A wildcard; one for columns (load_only()) leaves relationships as they are.
                wildcard = wildcard or any(_relationship_token(element) for element in path)
                continue
            key = _path_key(path)
            strategy = getattr(loader, "strategy", None)
            if strategy is not None:
                # defaultload() names a path without choosing its strategy.
                named.add(key)
            joined = strategy is not None and ("lazy", "joined") in strategy
            # contains_eager() loads from the statement's own join, which the rewrite filters.
            plain_options = set(loader.local_opts) <= {"innerjoin"}
            if not joined or not plain_options or len(path) < 3 or not isinstance(path[-2], RelationshipProperty):
                explicit.pop(key, None)
                continue
            if any(in_polymorphic_alias(element) for element in path):
                explicit.pop(key, None)
                continue
            explicit[key] = (
                path,
                loader.local_opts.get("innerjoin"),
                tuple(getattr(loader, "_extra_criteria", ())),
            )
    return explicit, named, wildcard


class _SecondaryOptions:
    """The loader options ``joined_secondary_options`` makes, one for each path."""

    def __init__(self, policy: SoftDeletePolicy, named: set[tuple[int, ...]], wildcard: bool) -> None:
        self.policy = policy
        self.named = named
        self.wildcard = wildcard
        self.options: list[Any] = []
        self.statement_specific = False
        self._added: set[tuple[int, ...]] = set()

    def add(self, path: _Path, innerjoin: Any, criteria: tuple[Any, ...]) -> None:
        """An option for the joined eager load ``path`` ends at, when its relationship's secondary is
        soft-deletable."""
        key = _path_key(path)
        condition = self.policy.secondary_condition(path[-2])
        if condition is None or key in self._added:
            return
        self._added.add(key)
        self.options.append(_joined_load_along(path, (*criteria, condition), innerjoin))
        self.statement_specific = self.statement_specific or any(isinstance(element, AliasedInsp) for element in path)

    def along_mapping(self, path: _Path) -> None:
        """Options for the lazy="joined" relationships of the class ``path`` ends at, and of their targets in
        turn."""
        if self.wildcard:
            return
        stack = [path]
        visited: set[Mapper[Any]] = set()
        while stack:
            current = stack.pop()
            mapper = current[-1].mapper
            if mapper in visited:
                continue
            visited.add(mapper)
            for prop in mapper.relationships:
                if ("lazy", "joined") not in prop.strategy_key:
                    continue
                extended = [*current, prop, prop.entity]
                if _path_key(extended) in self.named:
                    # An option of the statement speaks for it.
                    continue
                self.add(extended, None, ())
                stack.append(extended)


def reusable_options(options: list[Any], statement_specific: bool) -> tuple[Any, ...] | None:
    """``options`` for a plan, or None when they depend on the statement's own entities."""
    return None if statement_specific else tuple(options)


def _relationship_token(element: Any) -> bool:
    """Whether a loader path element is a wildcard token for relationships (``relationship:*``)."""
    return isinstance(element, str) and element.startswith("relationship:")


def _path_key(path: _Path) -> tuple[int, ...]:
    return tuple(id(element) for element in path)


def _joined_load_along(path: _Path, criteria: tuple[Any, ...], innerjoin: Any) -> Any:
    """A loader option that joined eager loads the last relationship of a loader path (``[entity, relationship,
    entity, ...]``) with ``criteria``, and leaves the strategy of the relationships before it as it is."""

    def entity_of(element: Any) -> Any:
        return element.entity if isinstance(element, AliasedInsp) else element.class_

    load = Load(entity_of(path[0]))
    for index in range(1, len(path) - 1, 2):
        prop, target = path[index], path[index + 1]
        attribute = getattr(entity_of(path[index - 1]), prop.key)
        if isinstance(target, AliasedInsp) or target.mapper is not prop.mapper:
            attribute = attribute.of_type(entity_of(target))
        if index == len(path) - 2:
            return load.joinedload(attribute.and_(*criteria), innerjoin=innerjoin)
        load = load.defaultload(attribute)
    return load
