"""This module is responsible for activating the query rewriter."""

from typing import List, Optional

from sqlalchemy.event import listens_for
from sqlalchemy.orm import ORMExecuteState, Session

from sqlalchemy_easy_softdelete.handler.rewriter import SoftDeleteQueryRewriter
from sqlalchemy_easy_softdelete.hook import IgnoredTable

global_rewriter: Optional[SoftDeleteQueryRewriter] = None


def activate_soft_delete_hook(
    deleted_field_name: str, disable_soft_delete_option_name: str, ignored_tables: List[IgnoredTable]
):
    """Activate an event hook to rewrite the queries."""

    global global_rewriter
    global_rewriter = SoftDeleteQueryRewriter(
        deleted_field_name=deleted_field_name,
        disable_soft_delete_option_name=disable_soft_delete_option_name,
        ignored_tables=ignored_tables,
    )

    # Enable Soft Delete on all Relationship Loads which implement SoftDeleteMixin
    @listens_for(Session, identifier="do_orm_execute")
    def soft_delete_execute(state: ORMExecuteState):
        if not state.is_select:
            return

        # Rewrite the statement
        adapted = global_rewriter.rewrite_statement(state.statement)

        # Replace the statement
        state.statement = adapted
