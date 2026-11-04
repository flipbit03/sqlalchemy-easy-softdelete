"""A many-to-many relationship through soft-deletable association rows."""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class STModelBase(DeclarativeBase):
    """ST = Secondary Tables"""


class STSoftDeleteMixin(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


class STParent(STModelBase, STSoftDeleteMixin):
    __tablename__ = "stparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tags: Mapped[list["STTag"]] = relationship(secondary="stlink", viewonly=True)


class STTag(STModelBase, STSoftDeleteMixin):
    __tablename__ = "sttag"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class STLink(STModelBase, STSoftDeleteMixin):
    """An association row between a parent and a tag."""

    __tablename__ = "stlink"
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("stparent.id"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(Integer, ForeignKey("sttag.id"), primary_key=True)
