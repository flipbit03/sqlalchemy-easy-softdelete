"""The classes a statement loads beyond its FROM clause, which loader criteria filter: those of eager loading, of
the relationships its mapping joins, and those the SELECTs in its columns and ``column_property()`` expressions
select from."""

from __future__ import annotations

import itertools
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Mapper
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.orm.strategy_options import Load
from sqlalchemy.orm.util import AliasedInsp
from sqlalchemy.sql import Select, visitors
from sqlalchemy.sql.selectable import FromClause

from sqlalchemy_easy_softdelete.handler.rewriter.analysis.nested import listed_selects, mappers_in
from sqlalchemy_easy_softdelete.handler.rewriter.context import RewriteContext
from sqlalchemy_easy_softdelete.handler.rewriter.plan import Criteria, Reason
from sqlalchemy_easy_softdelete.handler.rewriter.policy import SoftDeletePolicy, entity_class_of
from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals
from sqlalchemy_easy_softdelete.handler.rewriter.sa.clauses import (
    annotated_entity,
    column_property_selects,
    joins_of,
    subquery_in,
)


def loaded_mappers(stmt: Select[Any]) -> list[Mapper[Any]]:
    """The mappers of the entities ``stmt`` selects (classes and ``aliased()`` entities)."""
    return [
        insp.mapper
        for insp in (
            annotated_entity(column) for column in internals.raw_columns(stmt) if isinstance(column, FromClause)
        )
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
    for opt in internals.loader_options(stmt):
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
    ``transform.nested``). One that disables filtering is left out by the criteria (see ``SoftDeleteCriteria``).
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
                of_type = internals.of_type(attribute)
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
    for opt in internals.loader_options(stmt):
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
