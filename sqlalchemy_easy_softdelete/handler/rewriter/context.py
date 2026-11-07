"""What a rewrite depends on besides the statement and the configuration: where the statement being rewritten sits.

A statement's SELECTs are rewritten recursively (subqueries, CTEs, nested SELECTs, union members), and how each is
rewritten depends on what encloses it. ``RewriteContext`` carries that, explicitly: each level of the recursion
passes a derived context to the next one.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy_easy_softdelete.handler.rewriter.policy import SoftDeletePolicy

RELATIONSHIP_LOAD: contextvars.ContextVar[bool] = contextvars.ContextVar("relationship_load", default=False)
"""Kept for compatibility: a ``rewrite_statement()`` call without a context reads it (see
``RewriteContext.relationship_load``). The hook passes its context explicitly."""

LOAD_PATH: contextvars.ContextVar[Any] = contextvars.ContextVar("load_path", default=None)
"""Kept for compatibility: a ``rewrite_statement()`` call without a context reads it (see
``RewriteContext.load_path``). The hook passes its context explicitly."""

_NO_FROMS: frozenset[int] = frozenset()


@dataclass(frozen=True)
class RewriteContext:
    """Where the statement being rewritten sits. The root context of an execution comes from the hook."""

    relationship_load: bool = False
    """Whether the statement being rewritten loads a relationship (from ``ORMExecuteState.is_relationship_load``).
    Such a statement selects its parent rows by primary key, from an alias of the parent that SQLAlchemy makes:
    aliases of tables are not filtered there, so related rows of the parents a statement loaded on purpose with
    ``include_deleted`` still load."""

    load_path: Any = None
    """The loader path of the relationship load being rewritten (``ORMExecuteState.loader_strategy_path``).
    SQLAlchemy passes the parent statement's loader options on to each relationship load, those of other paths
    too: only the options on the load's own path concern its statement."""

    enclosing_froms: frozenset[int] = _NO_FROMS
    """The ids of the FROM elements of the statement the SELECT being rewritten is nested in (a SELECT in a WHERE
    clause): the nested SELECT correlates to them, so they are filtered there, not in it. Empty for a statement
    of its own (an execution's statement, or that of a subquery or CTE in a FROM clause)."""

    nested_key: Any = None
    """The key the plan of the nested SELECT being rewritten is remembered by: that of the statement it is nested
    in, and its place there, or None when that statement's plan is not remembered."""

    copy_memo: dict[int, Any] | None = None
    """The filtered copies made for the statement the SELECT being rewritten is nested in, by the id of the
    subquery or CTE each replaces: the nested SELECT uses the same ones, so that a CTE renders once."""

    in_lateral: bool = False
    """Whether the statement being rewritten is (inside) the statement of a LATERAL subquery: its references to the
    FROMs of the enclosing statement (another LATERAL subquery's columns) are correlations, not FROMs of its own."""

    ctes_in_rewrite: frozenset[int] = _NO_FROMS
    """The CTEs (by id) whose statement is being rewritten, with those they restate: inside a recursive CTE's
    statement, the references to the CTE itself are left as they are."""

    @classmethod
    def ambient(cls) -> RewriteContext:
        """The root context of a ``rewrite_statement()`` call that gives none: from the compatibility variables."""
        return cls(relationship_load=RELATIONSHIP_LOAD.get(), load_path=LOAD_PATH.get())

    def nested_in(self, froms: frozenset[int], memo: dict[int, Any]) -> RewriteContext:
        """For the SELECTs nested in a statement whose FROM elements are ``froms`` and whose copies are ``memo``."""
        return RewriteContext(
            self.relationship_load, self.load_path, froms, self.nested_key, memo, self.in_lateral, self.ctes_in_rewrite
        )

    def keyed(self, nested_key: Any) -> RewriteContext:
        """For a nested SELECT (or union member) whose plan is remembered by ``nested_key``."""
        return RewriteContext(
            self.relationship_load,
            self.load_path,
            self.enclosing_froms,
            nested_key,
            self.copy_memo,
            self.in_lateral,
            self.ctes_in_rewrite,
        )

    def unnested(self) -> RewriteContext:
        """For the statement of a subquery or CTE of a FROM clause: it correlates to nothing, and makes copies of
        its own."""
        return RewriteContext(
            self.relationship_load,
            self.load_path,
            _NO_FROMS,
            self.nested_key,
            None,
            self.in_lateral,
            self.ctes_in_rewrite,
        )

    def lateral(self) -> RewriteContext:
        """For what is inside a LATERAL subquery."""
        return RewriteContext(
            self.relationship_load,
            self.load_path,
            self.enclosing_froms,
            self.nested_key,
            self.copy_memo,
            True,
            self.ctes_in_rewrite,
        )

    def in_ctes(self, ids: set[int]) -> RewriteContext:
        """For the statement of a CTE: ``ids`` are the CTE and those it restates."""
        return RewriteContext(
            self.relationship_load,
            self.load_path,
            self.enclosing_froms,
            self.nested_key,
            self.copy_memo,
            self.in_lateral,
            self.ctes_in_rewrite | ids,
        )


class Rewriter(Protocol):
    """What the planner and the transformations need from the rewriter: its policy, and rewriting the statements
    nested in the one being rewritten."""

    policy: SoftDeletePolicy

    def rewrite_select(self, stmt: Any, context: RewriteContext | None = None) -> Any: ...

    def rewrite_compound_select(self, stmt: Any, context: RewriteContext | None = None) -> Any: ...
