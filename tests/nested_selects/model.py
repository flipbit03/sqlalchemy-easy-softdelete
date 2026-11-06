"""SELECTs nested in statements (EXISTS, IN, scalar subqueries) and in column_property() expressions."""

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, column_property, deferred, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class NSModelBase(DeclarativeBase):
    """NS = Nested Selects"""


class NSSoftDeleteMixin(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


class NSParent(NSModelBase, NSSoftDeleteMixin):
    __tablename__ = "nsparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    children: Mapped[list["NSChild"]] = relationship(back_populates="parent", order_by="NSChild.id")


class NSChild(NSModelBase, NSSoftDeleteMixin):
    __tablename__ = "nschild"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("nsparent.id"))
    parent: Mapped[NSParent] = relationship(back_populates="children")


class NSOwner(NSModelBase, NSSoftDeleteMixin):
    __tablename__ = "nsowner"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("nsparent.id"))
    parent: Mapped[NSParent] = relationship()


def _children_count() -> Any:
    return select(func.count(NSChild.id)).where(NSChild.parent_id == NSParent.id).correlate_except(NSChild)


NSParent.child_count = column_property(_children_count().scalar_subquery())  # type: ignore[attr-defined]
NSParent.every_child_count = column_property(  # type: ignore[attr-defined]
    _children_count().execution_options(include_deleted=True).scalar_subquery()
)
NSParent.deferred_child_count = deferred(_children_count().scalar_subquery())  # type: ignore[attr-defined]
