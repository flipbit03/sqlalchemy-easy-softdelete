"""This module is responsible for activating the query rewriter."""

from typing import cast

from sqlalchemy.event import listens_for
from sqlalchemy.orm import FromStatement, ORMExecuteState, Session
from sqlalchemy.sql import Executable

from sqlalchemy_easy_softdelete.handler.rewriter import (
    DEFAULT_REWRITE_CACHE_SIZE,
    RewriteContext,
    SoftDeleteQueryRewriter,
)
from sqlalchemy_easy_softdelete.hook import IgnoredTable


def activate_soft_delete_hook(
    deleted_field_name: str,
    disable_soft_delete_option_name: str,
    ignored_tables: list[IgnoredTable],
    rewrite_cache_size: int = DEFAULT_REWRITE_CACHE_SIZE,
) -> SoftDeleteQueryRewriter:
    """Activate an event hook to rewrite the queries.

    Returns the SoftDeleteQueryRewriter instance for use by the mixin class.
    """
    rewriter = SoftDeleteQueryRewriter(
        deleted_field_name=deleted_field_name,
        disable_soft_delete_option_name=disable_soft_delete_option_name,
        ignored_tables=ignored_tables,
        rewrite_cache_size=rewrite_cache_size,
    )

    # Enable Soft Delete on all Relationship Loads which implement SoftDeleteMixin
    @listens_for(Session, identifier="do_orm_execute")
    def soft_delete_execute(state: ORMExecuteState) -> None:
        # Before SQLAlchemy 2.0.30, is_select is False for select(...).from_statement(...), also inside a
        # lambda_stmt(). The rewriter leaves a from_statement() of INSERT/UPDATE/DELETE ... RETURNING or
        # of textual SQL as it is.
        if not state.is_select and not isinstance(
            getattr(state.statement, "_resolved", state.statement), FromStatement
        ):
            return

        # The option can also be given at execution time (Session.execute(..., execution_options=...),
        # Session.get()), which leaves it out of the statement's own options. SQLAlchemy passes the
        # options of an execution on to the loads it runs along with it (selectin, subquery and immediate
        # loads); lazy loads that happen later are executions of their own.
        if state.execution_options.get(disable_soft_delete_option_name):
            return

        # Rewrite the statement (closure captures local `rewriter`), as the execution's own: a relationship load
        # selects its parent rows by primary key, along its loader path.
        context = RewriteContext.root(
            state.is_relationship_load, state.loader_strategy_path if state.is_relationship_load else None
        )
        adapted = rewriter.rewrite_statement(state.statement, context)

        # Replace the statement
        # Cast needed because Statement type includes LambdaElement which mypy
        # doesn't recognize as Executable (even though it is at runtime)
        state.statement = cast(Executable, adapted)

    return rewriter
