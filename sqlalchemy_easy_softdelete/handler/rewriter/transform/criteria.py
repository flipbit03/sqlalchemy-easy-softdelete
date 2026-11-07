"""Loader criteria: the soft-delete filter SQLAlchemy applies to every SELECT of a class compiled with the statement
(eager loads, the SELECTs in its columns and ``column_property()`` expressions), and how they pickle."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Mapper
from sqlalchemy.orm.util import AliasedInsp, LoaderCriteriaOption
from sqlalchemy.sql import CompoundSelect, Select, visitors
from sqlalchemy.sql.selectable import FromClause
from sqlalchemy.sql.visitors import InternalTraversal

from sqlalchemy_easy_softdelete.handler.rewriter.criteria_functions import PICKLE_MODULE
from sqlalchemy_easy_softdelete.handler.rewriter.plan import Criteria
from sqlalchemy_easy_softdelete.handler.rewriter.policy import Configuration, SoftDeletePolicy
from sqlalchemy_easy_softdelete.handler.rewriter.sa import internals
from sqlalchemy_easy_softdelete.handler.rewriter.sa.clauses import add_selects, annotated_entity
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
            for insp in (
                annotated_entity(column) for column in internals.raw_columns(stmt) if isinstance(column, FromClause)
            )
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
        *internals.LOADER_CRITERIA_TRAVERSE_INTERNALS,
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
                or internals.is_marked_filtered(statement)
                or internals.execution_option(statement, self.disable_option_name)
            ):
                return False
            statement = internals.is_clone_of(statement)
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


# Pickle stores these functions by module and name: the rewriter's module before it became a package, which
# instances pickled by earlier versions refer to, and which re-exports them.
_unpickle_mapped_subquery_criteria.__module__ = PICKLE_MODULE
_unpickle_soft_delete_criteria.__module__ = PICKLE_MODULE
