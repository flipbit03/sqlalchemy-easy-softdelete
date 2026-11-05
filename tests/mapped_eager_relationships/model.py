"""Relationships that are eager by their mapping (``lazy="joined"``), with no loader option."""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, select
from sqlalchemy.orm import DeclarativeBase, Mapped, foreign, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class MEModelBase(DeclarativeBase):
    """ME = Mapped Eager relationships"""


class MESoftDeleteMixin(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


class MEParent(MEModelBase, MESoftDeleteMixin):
    __tablename__ = "meparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    children: Mapped[list["MEChild"]] = relationship(lazy="joined", order_by="MEChild.id", viewonly=True)


class MEChild(MEModelBase, MESoftDeleteMixin):
    __tablename__ = "mechild"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("meparent.id"))
    toys: Mapped[list["METoy"]] = relationship(lazy="joined", order_by="METoy.id", viewonly=True)


class METoy(MEModelBase, MESoftDeleteMixin):
    __tablename__ = "metoy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(Integer, ForeignKey("mechild.id"))


class MEInnerParent(MEModelBase, MESoftDeleteMixin):
    """Eager loads its children with an inner join: it is loaded only with some."""

    __tablename__ = "meinnerparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    children: Mapped[list["MEInnerChild"]] = relationship(
        lazy="joined", innerjoin=True, order_by="MEInnerChild.id", viewonly=True
    )


class MEInnerChild(MEModelBase, MESoftDeleteMixin):
    __tablename__ = "meinnerchild"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("meinnerparent.id"))


class MENode(MEModelBase, MESoftDeleteMixin):
    __tablename__ = "menode"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("menode.id"))
    kids: Mapped[list["MENode"]] = relationship(lazy="joined", join_depth=2, order_by="MENode.id", viewonly=True)


class MEHolder(MEModelBase, MESoftDeleteMixin):
    __tablename__ = "meholder"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("meparent.id"))
    parent: Mapped[MEParent] = relationship(viewonly=True)


child = MEChild.__table__
child_names = select(child.c.id, child.c.parent_id).subquery("mechild_names")


class MEChildName(MEModelBase):
    """The children's ids and parents, without their soft-delete column."""

    __table__ = child_names
    __mapper_args__ = {"primary_key": [__table__.c.id]}


MEParent.child_names = relationship(  # type: ignore[attr-defined]
    MEChildName,
    primaryjoin=lambda: MEParent.id == foreign(child_names.c.parent_id),
    lazy="joined",
    order_by=lambda: child_names.c.id,
    viewonly=True,
)
