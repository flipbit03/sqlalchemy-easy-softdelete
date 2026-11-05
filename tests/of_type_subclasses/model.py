"""Relationships to the base class of single-table and joined-table inheritance hierarchies."""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class OTModelBase(DeclarativeBase):
    """OT = Of_Type subclasses"""


class OTSoftDeleteMixin(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


class OTParent(OTModelBase, OTSoftDeleteMixin):
    __tablename__ = "otparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    children: Mapped[list["OTChild"]] = relationship(order_by="OTChild.id", viewonly=True)
    special_children: Mapped[list["OTSpecialChild"]] = relationship(order_by="OTSpecialChild.id", viewonly=True)
    items: Mapped[list["OTItem"]] = relationship(order_by="OTItem.id", viewonly=True)


class OTChild(OTModelBase, OTSoftDeleteMixin):
    """Single-table inheritance."""

    __tablename__ = "otchild"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("otparent.id"))
    kind: Mapped[str] = mapped_column(String(10))
    __mapper_args__ = {"polymorphic_on": kind, "polymorphic_identity": "child"}


class OTSpecialChild(OTChild):
    __mapper_args__ = {"polymorphic_identity": "special"}


class OTLeafChild(OTSpecialChild):
    __mapper_args__ = {"polymorphic_identity": "leaf"}


class OTItem(OTModelBase, OTSoftDeleteMixin):
    """Joined-table inheritance."""

    __tablename__ = "otitem"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("otparent.id"))
    kind: Mapped[str] = mapped_column(String(10))
    __mapper_args__ = {"polymorphic_on": kind, "polymorphic_identity": "item"}


class OTSpecialItem(OTItem):
    __tablename__ = "otspecialitem"
    id: Mapped[int] = mapped_column(Integer, ForeignKey("otitem.id"), primary_key=True)
    __mapper_args__ = {"polymorphic_identity": "special"}
