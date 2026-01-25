from datetime import datetime, timezone

from sqlalchemy import Column, Integer
from sqlalchemy.orm import Mapped, as_declarative

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class CDVModelBase:
    """CDV = Custom Default Value"""

    id = Column(Integer, primary_key=True, autoincrement=True)


class CDVSoftDeleteMixin(
    generate_soft_delete_mixin_class(  # type: ignore[misc]
        delete_method_default_value=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
):
    deleted_at: Mapped[datetime | None]


class CDVTable(CDVModelBase, CDVSoftDeleteMixin):
    __tablename__ = "cdvtable"
    value = Column(Integer)
