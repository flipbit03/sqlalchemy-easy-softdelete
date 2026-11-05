"""Relationships that are eager by their mapping (``lazy="joined"``), with no loader option."""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, aliased, foreign, mapped_column, relationship

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


# Children through aliases of their class over subqueries the eager loading criteria must not filter.
every_child = aliased(MEChild, select(MEChild).execution_options(include_deleted=True).subquery())
child_ids = aliased(MEChild, select(child.c.id, child.c.parent_id).subquery())


class MEArchive(MEModelBase, MESoftDeleteMixin):
    """Eager loads every child of the parent with its id, soft-deleted ones included."""

    __tablename__ = "mearchive"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    every_child: Mapped[list[MEChild]] = relationship(
        every_child,
        primaryjoin=lambda: MEArchive.id == foreign(every_child.parent_id),
        lazy="joined",
        order_by=lambda: every_child.id,
        viewonly=True,
    )


MEParent.children_by_id = relationship(  # type: ignore[attr-defined]
    child_ids,
    primaryjoin=lambda: MEParent.id == foreign(child_ids.parent_id),
    order_by=lambda: child_ids.id,
    viewonly=True,
)


class MEEmployee(MEModelBase, MESoftDeleteMixin):
    """Joined-table inheritance: the subclass's lazy="joined" relationship is joined when the base class is loaded
    polymorphically."""

    __tablename__ = "meemployee"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    __mapper_args__ = {"polymorphic_on": kind, "polymorphic_identity": "employee"}


class MEManager(MEEmployee):
    __tablename__ = "memanager"
    id: Mapped[int] = mapped_column(Integer, ForeignKey("meemployee.id"), primary_key=True)
    reports: Mapped[list["MEReport"]] = relationship(lazy="joined", order_by="MEReport.id", viewonly=True)
    __mapper_args__ = {"polymorphic_identity": "manager", "polymorphic_load": "inline"}


class MEReport(MEModelBase, MESoftDeleteMixin):
    __tablename__ = "mereport"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manager_id: Mapped[int] = mapped_column(Integer, ForeignKey("memanager.id"))
