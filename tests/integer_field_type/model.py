"""Models for testing integer field type option."""

from datetime import datetime, timezone

from sqlalchemy import Column, Integer
from sqlalchemy.orm import Mapped, as_declarative, declared_attr

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class IFTModelBase:
    """IFT = Integer Field Type"""

    @declared_attr
    def __tablename__(cls) -> str:
        return cls.__name__.lower()

    id = Column(Integer, primary_key=True, autoincrement=True)


class IFTSoftDeleteMixin(
    generate_soft_delete_mixin_class(
        deleted_field_type=Integer(),
        delete_method_default_value=lambda: int(datetime.now(timezone.utc).timestamp()),
    )
):
    deleted_at: Mapped[int | None]


class IFTTable(IFTModelBase, IFTSoftDeleteMixin):
    value = Column(Integer)
