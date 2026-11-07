"""Models and data for ``golden_pickles.py``: pickled instances refer to these classes by this module's name."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, foreign, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class GPBase(DeclarativeBase):
    """GP = Golden Pickles"""


class GPSoftDelete(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


class GPParent(GPBase, GPSoftDelete):
    __tablename__ = "gp_parent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    children: Mapped[list[GPChild]] = relationship(order_by="GPChild.id", viewonly=True)
    lazy_children: Mapped[list[GPChild]] = relationship(order_by="GPChild.id", viewonly=True)


class GPChild(GPBase, GPSoftDelete):
    __tablename__ = "gp_child"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("gp_parent.id"))


_child = GPChild.__table__
_child_names = select(_child.c.id, _child.c.parent_id).subquery("gp_child_name")


class GPChildName(GPBase):
    """A class mapped to a SELECT that leaves out the soft-delete column: filtered through its primary key."""

    __table__ = _child_names
    __mapper_args__ = {"primary_key": [_child_names.c.id]}


GPParent.child_names = relationship(  # type: ignore[attr-defined]
    GPChildName,
    primaryjoin=lambda: GPParent.id == foreign(_child_names.c.parent_id),
    order_by=lambda: _child_names.c.id,
    viewonly=True,
)


def populate(engine: Engine) -> None:
    """Parent 1 has children 1 (active) and 2 (soft-deleted)."""
    GPBase.metadata.create_all(engine)
    with Session(engine) as session:
        deleted = datetime(2020, 1, 1, tzinfo=timezone.utc)
        session.add_all([GPParent(id=1), GPChild(id=1, parent_id=1), GPChild(id=2, parent_id=1, deleted_at=deleted)])
        session.commit()
