"""Functions related to dynamic generation of the soft-delete mixin."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional, Type

from sqlalchemy import Column, DateTime
from sqlalchemy.sql.type_api import TypeEngine

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
    delete_method_default_value: Callable[[], Any] = lambda: datetime.utcnow(),
    generate_undelete_method: bool = True,
    undelete_method_name: str = "undelete",
) -> Type:
    """Generate the actual soft-delete Mixin class."""
    if not ignored_tables:
        ignored_tables = []

    class_attributes = {deleted_field_name: Column(deleted_field_name, deleted_field_type)}

    if generate_delete_method:

        def delete_method(_self, v: Optional[Any] = None):
            setattr(_self, deleted_field_name, v or delete_method_default_value())

        class_attributes[delete_method_name] = delete_method

    if generate_undelete_method:

        def undelete_method(_self):
            setattr(_self, deleted_field_name, None)

        class_attributes[undelete_method_name] = undelete_method

    activate_soft_delete_hook(deleted_field_name, disable_soft_delete_filtering_option_name, ignored_tables)

    generated_class = type(class_name, tuple(), class_attributes)

    return generated_class
