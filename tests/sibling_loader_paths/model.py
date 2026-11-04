"""A class with two relationships to the same class, for loader options on sibling paths."""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class SLModelBase(DeclarativeBase):
    """SL = Sibling Loads"""


class SLSoftDeleteMixin(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


class SLParent(SLModelBase, SLSoftDeleteMixin):
    __tablename__ = "slparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    children: Mapped[list["SLChild"]] = relationship(order_by="SLChild.id")
    kids: Mapped[list["SLChild"]] = relationship(viewonly=True, order_by="SLChild.id")


class SLChild(SLModelBase, SLSoftDeleteMixin):
    __tablename__ = "slchild"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("slparent.id"))
