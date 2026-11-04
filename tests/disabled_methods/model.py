from datetime import datetime

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, as_declarative, mapped_column

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class DMModelBase:
    """DM = Disabled Methods"""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class DMSoftDeleteMixin(
    generate_soft_delete_mixin_class(  # type: ignore[misc]
        generate_delete_method=False,
        generate_undelete_method=False,
    )
):
    deleted_at: Mapped[datetime | None]


class DMTable(DMModelBase, DMSoftDeleteMixin):
    __tablename__ = "dmtable"
    value: Mapped[int | None] = mapped_column(Integer)
