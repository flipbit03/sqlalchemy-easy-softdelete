"""Classes whose mapped selectable is a subquery: a concrete inheritance hierarchy (a polymorphic union)
and a class mapped to a SELECT."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.ext.declarative import ConcreteBase
from sqlalchemy.orm import Mapped, as_declarative, mapped_column

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class MSModelBase:
    """MS = Mapped Selectables"""


class MSSoftDeleteMixin(generate_soft_delete_mixin_class(deleted_field_name="erased_at")):  # type: ignore[misc]
    erased_at: Mapped[datetime | None]


class MSEmployee(ConcreteBase, MSModelBase, MSSoftDeleteMixin):
    __tablename__ = "msemployee"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(50))
    __mapper_args__ = {"polymorphic_identity": "employee", "concrete": True}


class MSManager(MSEmployee):
    __tablename__ = "msmanager"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(50))
    # A concrete subclass maps its own table, so it declares the soft-delete column itself.
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __mapper_args__ = {"polymorphic_identity": "manager", "concrete": True}


class MSItem(MSModelBase, MSSoftDeleteMixin):
    __tablename__ = "msitem"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(50))


item = MSItem.__table__


class MSItemView(MSModelBase):
    """Mapped to a SELECT of the soft-deletable table."""

    __table__ = select(item.c.id, item.c.name, item.c.erased_at).subquery("msitem_view")
    __mapper_args__ = {"primary_key": [__table__.c.id]}


class MSItemName(MSModelBase):
    """Mapped to a SELECT of the soft-deletable table that leaves out its soft-delete column."""

    __table__ = select(item.c.id, item.c.name).subquery("msitem_name")
    __mapper_args__ = {"primary_key": [__table__.c.id]}
