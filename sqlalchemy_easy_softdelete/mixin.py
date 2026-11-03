"""Functions related to dynamic generation of the soft-delete mixin."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, DateTime
from sqlalchemy.sql.type_api import TypeEngine

from sqlalchemy_easy_softdelete.handler.rewriter import DEFAULT_REWRITE_CACHE_SIZE
from sqlalchemy_easy_softdelete.handler.sqlalchemy_easy_softdelete import activate_soft_delete_hook
from sqlalchemy_easy_softdelete.hook import IgnoredTable


def generate_soft_delete_mixin_class(
    deleted_field_name: str = "deleted_at",
    ignored_tables: list[IgnoredTable] | None = None,
    class_name: str = "_SoftDeleteMixin",
    deleted_field_type: TypeEngine = DateTime(timezone=True),
    disable_soft_delete_filtering_option_name: str = "include_deleted",
    generate_delete_method: bool = True,
    delete_method_name: str = "delete",
    delete_method_default_value: Callable[[], Any] = lambda: datetime.now(timezone.utc),
    generate_undelete_method: bool = True,
    undelete_method_name: str = "undelete",
    rewrite_cache_size: int = DEFAULT_REWRITE_CACHE_SIZE,
) -> type:
    """Generate the actual soft-delete Mixin class.

    ``rewrite_cache_size`` is how many statement shapes the query rewriter remembers the rewrite of,
    so that later statements of the same shape skip the analysis of their FROM clause (0 disables it).
    """
    if not ignored_tables:
        ignored_tables = []

    class_attributes: dict[str, Any] = {deleted_field_name: Column(deleted_field_name, deleted_field_type)}

    if generate_delete_method:

        def delete_method(_self, v: Any | None = None):
            setattr(_self, deleted_field_name, v or delete_method_default_value())

        class_attributes[delete_method_name] = delete_method

    if generate_undelete_method:

        def undelete_method(_self):
            setattr(_self, deleted_field_name, None)

        class_attributes[undelete_method_name] = undelete_method

    # Activate the soft delete hook and get the rewriter instance
    rewriter = activate_soft_delete_hook(
        deleted_field_name,
        disable_soft_delete_filtering_option_name,
        ignored_tables,
        rewrite_cache_size=rewrite_cache_size,
    )

    # Store the rewriter on the generated class: tests use it, and it reports the rewrite cache's
    # statistics (`_sqlalchemy_easy_softdelete_rewriter.rewrite_cache_info()`).
    class_attributes["_sqlalchemy_easy_softdelete_rewriter"] = rewriter

    generated_class = type(class_name, tuple(), class_attributes)

    return generated_class
