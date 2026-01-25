"""Models for testing custom default value option."""

from datetime import datetime, timezone

from sqlalchemy import Column, Integer
from sqlalchemy.orm import Mapped, as_declarative, declared_attr

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class CDVModelBase:
    """CDV = Custom Default Value"""

    @declared_attr
    def __tablename__(cls) -> str:
        return cls.__name__.lower()

    id = Column(Integer, primary_key=True, autoincrement=True)


class CDVSoftDeleteMixin(
    generate_soft_delete_mixin_class(
        delete_method_default_value=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
):
    deleted_at: Mapped[datetime | None]


class CDVTable(CDVModelBase, CDVSoftDeleteMixin):
    value = Column(Integer)
