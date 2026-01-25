from datetime import datetime

from sqlalchemy import Column, Integer
from sqlalchemy.orm import Mapped, as_declarative

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class CFNModelBase:
    """CFN = Custom Field Name"""

    id = Column(Integer, primary_key=True, autoincrement=True)


class CFNSoftDeleteMixin(
    generate_soft_delete_mixin_class(  # type: ignore[misc]
        deleted_field_name="removed_at",
    )
):
    removed_at: Mapped[datetime | None]


class CFNTable(CFNModelBase, CFNSoftDeleteMixin):
    __tablename__ = "cfntable"
    value = Column(Integer)
