"""A class mapped to a SELECT that outer joins a soft-deletable table, without the soft-delete columns."""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, select
from sqlalchemy.orm import DeclarativeBase, Mapped, foreign, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class OJModelBase(DeclarativeBase):
    """OJ = Outer Joined subqueries"""


class OJSoftDeleteMixin(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


class OJParent(OJModelBase, OJSoftDeleteMixin):
    __tablename__ = "ojparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class OJChild(OJModelBase, OJSoftDeleteMixin):
    __tablename__ = "ojchild"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("ojparent.id"))


class OJNickname(OJModelBase, OJSoftDeleteMixin):
    __tablename__ = "ojnickname"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(Integer, ForeignKey("ojchild.id"))


class OJBadge(OJModelBase, OJSoftDeleteMixin):
    __tablename__ = "ojbadge"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nickname_id: Mapped[int] = mapped_column(Integer, ForeignKey("ojnickname.id"))


child = OJChild.__table__
nickname = OJNickname.__table__
# The children with their nickname, if any.
child_nicknames = (
    select(child.c.id, child.c.parent_id, nickname.c.id.label("nickname_id"))
    .select_from(child.outerjoin(nickname, nickname.c.child_id == child.c.id))
    .subquery("ojchild_nicknames")
)


class OJChildNickname(OJModelBase):
    __table__ = child_nicknames
    __mapper_args__ = {"primary_key": [__table__.c.id]}


OJParent.child_nicknames = relationship(  # type: ignore[attr-defined]
    OJChildNickname,
    primaryjoin=lambda: OJParent.id == foreign(child_nicknames.c.parent_id),
    viewonly=True,
    order_by=lambda: child_nicknames.c.id,
)

# The badges of each parent's children's nicknames, through the children with their nickname.
OJParent.badges = relationship(  # type: ignore[attr-defined]
    OJBadge,
    secondary=child_nicknames,
    primaryjoin=lambda: OJParent.id == child_nicknames.c.parent_id,
    secondaryjoin=lambda: child_nicknames.c.nickname_id == OJBadge.nickname_id,
    viewonly=True,
    order_by=lambda: OJBadge.id,
)
