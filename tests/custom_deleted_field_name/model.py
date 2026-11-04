from datetime import datetime

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, as_declarative, mapped_column

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class CFNModelBase:
    """CFN = Custom Field Name"""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class CFNSoftDeleteMixin(
    generate_soft_delete_mixin_class(  # type: ignore[misc]
        deleted_field_name="removed_at",
    )
):
    removed_at: Mapped[datetime | None]


class CFNTable(CFNModelBase, CFNSoftDeleteMixin):
    __tablename__ = "cfntable"
    value: Mapped[int | None] = mapped_column(Integer)
