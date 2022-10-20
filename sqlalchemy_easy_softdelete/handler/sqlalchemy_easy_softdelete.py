"""This module is responsible for activating the query rewriter."""

from functools import cache

from sqlalchemy.event import listens_for
from sqlalchemy.orm import ORMExecuteState, Session

from sqlalchemy_easy_softdelete.handler.rewriter import SoftDeleteQueryRewriter


@cache
def activate_soft_delete_hook(deleted_field_name: str, disable_soft_delete_option_name: str):
    """Activate an event hook to rewrite the queries."""
    # Enable Soft Delete on all Relationship Loads which implement SoftDeleteMixin
    @listens_for(Session, "do_orm_execute")
    def soft_delete_execute(state: ORMExecuteState):
        if not state.is_select:
            return

        adapted = SoftDeleteQueryRewriter(deleted_field_name, disable_soft_delete_option_name).rewrite_statement(
            state.statement
        )
        state.statement = adapted
