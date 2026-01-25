"""Models for testing custom method names option."""

from datetime import datetime

from sqlalchemy import Column, Integer
from sqlalchemy.orm import Mapped, as_declarative, declared_attr

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class CMNModelBase:
    """CMN = Custom Method Names"""

    @declared_attr
    def __tablename__(cls) -> str:
        return cls.__name__.lower()

    id = Column(Integer, primary_key=True, autoincrement=True)


class CMNSoftDeleteMixin(
    generate_soft_delete_mixin_class(
        delete_method_name="soft_delete",
        undelete_method_name="restore",
    )
):
    deleted_at: Mapped[datetime | None]

    def soft_delete(self) -> None:
        super().soft_delete()  # type: ignore[misc]

    def restore(self) -> None:
        super().restore()  # type: ignore[misc]


class CMNTable(CMNModelBase, CMNSoftDeleteMixin):
    value = Column(Integer)
