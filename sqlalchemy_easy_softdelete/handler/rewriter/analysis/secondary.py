"""Relationships whose secondary is soft-deletable: the condition their joins get, and the loader options that
repeat their joined eager loads with it."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Mapper, RelationshipProperty
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.orm.strategy_options import Load
from sqlalchemy.orm.util import AliasedInsp
from sqlalchemy.sql import Select
from sqlalchemy.sql.selectable import FromClause

from sqlalchemy_easy_softdelete.handler.rewriter.policy import SoftDeletePolicy
from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals
from sqlalchemy_easy_softdelete.handler.rewriter.sa.clauses import annotated_entity, joins_of

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
    for column in internals.raw_columns(stmt):
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
    for opt in internals.loader_options(stmt):
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
            if any(internals.in_polymorphic_alias(element) for element in path):
                explicit.pop(key, None)
                continue
            explicit[key] = (
                path,
                loader.local_opts.get("innerjoin"),
                internals.extra_criteria(loader),
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
