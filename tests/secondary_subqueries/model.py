"""Relationships whose secondary is a SELECT of a soft-deletable table."""

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Relationship, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class SSModelBase(DeclarativeBase):
    """SS = Secondary Subqueries"""


class SSSoftDeleteMixin(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


class SSParent(SSModelBase, SSSoftDeleteMixin):
    __tablename__ = "ssparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class SSChild(SSModelBase, SSSoftDeleteMixin):
    __tablename__ = "sschild"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("ssparent.id"))


class SSGrandChild(SSModelBase, SSSoftDeleteMixin):
    __tablename__ = "ssgrandchild"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(Integer, ForeignKey("sschild.id"))


class SSNickname(SSModelBase, SSSoftDeleteMixin):
    __tablename__ = "ssnickname"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(Integer, ForeignKey("sschild.id"))


child = SSChild.__table__
nickname = SSNickname.__table__
# The children's ids and parents, without their soft-delete column.
child_links = select(child.c.id, child.c.parent_id).subquery("child_links")
# The same with the soft-delete column.
child_rows = select(child.c.id, child.c.parent_id, child.c.deleted_at).subquery("child_rows")
# The children with their nickname, if any: the nickname's primary key, without its soft-delete column.
child_nicknames = (
    select(child.c.id, child.c.parent_id, nickname.c.id.label("nickname_id"))
    .select_from(child.outerjoin(nickname, nickname.c.child_id == child.c.id))
    .subquery("child_nicknames")
)
# Every child, soft-deleted ones included.
every_child = select(child.c.id, child.c.parent_id).execution_options(include_deleted=True).subquery("every_child")


def _grandchildren_through(secondary: Any) -> Relationship[Any]:
    return relationship(
        SSGrandChild,
        secondary=secondary,
        primaryjoin=SSParent.id == secondary.c.parent_id,
        secondaryjoin=secondary.c.id == SSGrandChild.child_id,
        viewonly=True,
        order_by=SSGrandChild.id,
    )


SSParent.grandchildren = _grandchildren_through(child_links)  # type: ignore[attr-defined]
SSParent.grandchildren_by_rows = _grandchildren_through(child_rows)  # type: ignore[attr-defined]
SSParent.all_grandchildren = _grandchildren_through(every_child)
SSParent.grandchildren_by_nickname = _grandchildren_through(child_nicknames)  # type: ignore[attr-defined]
