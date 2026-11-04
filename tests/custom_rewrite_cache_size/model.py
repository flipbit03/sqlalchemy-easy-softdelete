from datetime import datetime

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, as_declarative, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class CRCModelBase:
    """CRC = Custom Rewrite Cache"""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class CRCSoftDeleteMixin(
    generate_soft_delete_mixin_class(  # type: ignore[misc]
        deleted_field_name="archived_at",
        rewrite_cache_size=2000,
    )
):
    archived_at: Mapped[datetime | None]


class CRCParent(CRCModelBase, CRCSoftDeleteMixin):
    __tablename__ = "crcparent"
    children: Mapped[list["CRCChild"]] = relationship("CRCChild", back_populates="parent")  # type: ignore[assignment]


class CRCChild(CRCModelBase, CRCSoftDeleteMixin):
    __tablename__ = "crcchild"
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("crcparent.id"), nullable=False)
    parent: Mapped["CRCParent"] = relationship("CRCParent", back_populates="children")  # type: ignore[assignment]
