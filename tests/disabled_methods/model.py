"""Models for testing disabled methods option."""

from datetime import datetime

from sqlalchemy import Column, Integer
from sqlalchemy.orm import Mapped, as_declarative, declared_attr

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class DMModelBase:
    """DM = Disabled Methods"""

    @declared_attr
    def __tablename__(cls) -> str:
        return cls.__name__.lower()

    id = Column(Integer, primary_key=True, autoincrement=True)


class DMSoftDeleteMixin(
    generate_soft_delete_mixin_class(
        generate_delete_method=False,
        generate_undelete_method=False,
    )
):
    deleted_at: Mapped[datetime | None]


class DMTable(DMModelBase, DMSoftDeleteMixin):
    value = Column(Integer)
