from datetime import datetime

from sqlalchemy import Column, Integer
from sqlalchemy.orm import Mapped, as_declarative

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class CMNModelBase:
    """CMN = Custom Method Names"""

    id = Column(Integer, primary_key=True, autoincrement=True)


class CMNSoftDeleteMixin(
    generate_soft_delete_mixin_class(  # type: ignore[misc]
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
    __tablename__ = "cmntable"
    value = Column(Integer)
