from datetime import datetime

from sqlalchemy import Column, Integer
from sqlalchemy.orm import Mapped, as_declarative

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class DMModelBase:
    """DM = Disabled Methods"""

    id = Column(Integer, primary_key=True, autoincrement=True)


class DMSoftDeleteMixin(
    generate_soft_delete_mixin_class(  # type: ignore[misc]
        generate_delete_method=False,
        generate_undelete_method=False,
    )
):
    deleted_at: Mapped[datetime | None]


class DMTable(DMModelBase, DMSoftDeleteMixin):
    __tablename__ = "dmtable"
    value = Column(Integer)
