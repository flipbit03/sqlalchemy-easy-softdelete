from datetime import datetime, timezone

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, as_declarative, mapped_column

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class IFTModelBase:
    """IFT = Integer Field Type"""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class IFTSoftDeleteMixin(
    generate_soft_delete_mixin_class(  # type: ignore[misc]
        deleted_field_type=Integer(),
        delete_method_default_value=lambda: int(datetime.now(timezone.utc).timestamp()),
    )
):
    deleted_at: Mapped[int | None]


class IFTTable(IFTModelBase, IFTSoftDeleteMixin):
    __tablename__ = "ifttable"
    value: Mapped[int | None] = mapped_column(Integer)
