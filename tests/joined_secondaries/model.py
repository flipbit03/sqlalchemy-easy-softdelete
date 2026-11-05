"""Relationships through soft-deletable secondary tables and subqueries, joined eager loaded."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class JSModelBase(DeclarativeBase):
    """JS = Joined Secondaries"""


class JSSoftDeleteMixin(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


link = Table(
    "jslink",
    JSModelBase.metadata,
    Column("parent_id", Integer, ForeignKey("jsparent.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("jstag.id"), primary_key=True),
    Column("deleted_at", DateTime(timezone=True)),
)
item_link = Table(
    "jsitemlink",
    JSModelBase.metadata,
    Column("item_id", Integer, ForeignKey("jsitem.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("jstag.id"), primary_key=True),
    Column("deleted_at", DateTime(timezone=True)),
)
# The links, through a SELECT that passes on their soft-delete column.
link_rows = select(link.c.parent_id, link.c.tag_id, link.c.deleted_at).subquery("jslink_rows")


class JSOwner(JSModelBase, JSSoftDeleteMixin):
    __tablename__ = "jsowner"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parents: Mapped[list["JSParent"]] = relationship(order_by="JSParent.id", viewonly=True)
    eager_parents: Mapped[list["JSEagerParent"]] = relationship(order_by="JSEagerParent.id", viewonly=True)
    items: Mapped[list["JSItem"]] = relationship(order_by="JSItem.id", viewonly=True)


class JSParent(JSModelBase, JSSoftDeleteMixin):
    __tablename__ = "jsparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("jsowner.id"))
    tags: Mapped[list["JSTag"]] = relationship(secondary=link, order_by="JSTag.id", viewonly=True)
    tags_by_rows: Mapped[list["JSTag"]] = relationship(
        secondary=link_rows,
        primaryjoin=lambda: JSParent.id == link_rows.c.parent_id,
        secondaryjoin=lambda: link_rows.c.tag_id == JSTag.id,
        order_by="JSTag.id",
        viewonly=True,
    )


class JSEagerParent(JSModelBase, JSSoftDeleteMixin):
    """The same links, eager by its mapping."""

    __tablename__ = "jseagerparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("jsowner.id"))
    tags: Mapped[list["JSTag"]] = relationship(
        secondary=link,
        primaryjoin=lambda: JSEagerParent.id == link.c.parent_id,
        secondaryjoin=lambda: link.c.tag_id == JSTag.id,
        lazy="joined",
        order_by="JSTag.id",
        viewonly=True,
    )


class JSTag(JSModelBase, JSSoftDeleteMixin):
    __tablename__ = "jstag"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class JSItem(JSModelBase, JSSoftDeleteMixin):
    __tablename__ = "jsitem"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("jsowner.id"))
    kind: Mapped[str] = mapped_column(String(10))
    __mapper_args__ = {"polymorphic_on": kind, "polymorphic_identity": "item"}


class JSSpecialItem(JSItem):
    __tablename__ = "jsspecialitem"
    id: Mapped[int] = mapped_column(Integer, ForeignKey("jsitem.id"), primary_key=True)
    tags: Mapped[list[JSTag]] = relationship(secondary=item_link, order_by="JSTag.id", viewonly=True)
    __mapper_args__ = {"polymorphic_identity": "special"}
