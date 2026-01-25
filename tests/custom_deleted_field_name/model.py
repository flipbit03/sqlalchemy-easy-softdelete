"""Models for testing custom deleted_field_name option."""

from datetime import datetime

from sqlalchemy import Column, Integer
from sqlalchemy.orm import Mapped, as_declarative, declared_attr

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class CFNModelBase:
    """CFN = Custom Field Name"""

    @declared_attr
    def __tablename__(cls) -> str:
        return cls.__name__.lower()

    id = Column(Integer, primary_key=True, autoincrement=True)


class CFNSoftDeleteMixin(
    generate_soft_delete_mixin_class(
        deleted_field_name="removed_at",
    )
):
    removed_at: Mapped[datetime | None]


class CFNTable(CFNModelBase, CFNSoftDeleteMixin):
    value = Column(Integer)
