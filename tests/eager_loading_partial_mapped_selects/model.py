"""Classes mapped to SELECTs of soft-deletable tables that leave out the soft-delete column."""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, select
from sqlalchemy.orm import DeclarativeBase, Mapped, foreign, mapped_column, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class PMModelBase(DeclarativeBase):
    """PM = Partial Mapped selects"""


class PMSoftDeleteMixin(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


class PMParent(PMModelBase, PMSoftDeleteMixin):
    __tablename__ = "pmparent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class PMChild(PMModelBase, PMSoftDeleteMixin):
    __tablename__ = "pmchild"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("pmparent.id"))


child = PMChild.__table__
parent = PMParent.__table__
child_name = select(child.c.id, child.c.parent_id).subquery("pmchild_name")
child_with_parent = (
    select(child.c.id, child.c.parent_id, parent.c.id.label("parent_row_id"))
    .join_from(child, parent, child.c.parent_id == parent.c.id)
    .subquery("pmchild_with_parent")
)


class PMChildName(PMModelBase):
    """The children's ids and parents, without their soft-delete column."""

    __table__ = child_name
    __mapper_args__ = {"primary_key": [__table__.c.id]}


class PMChildWithParent(PMModelBase):
    """The children with their parents (a join of two soft-deletable tables), without a soft-delete column."""

    __table__ = child_with_parent
    __mapper_args__ = {"primary_key": [__table__.c.id]}


PMParent.child_names = relationship(  # type: ignore[attr-defined]
    PMChildName,
    primaryjoin=lambda: PMParent.id == foreign(child_name.c.parent_id),
    viewonly=True,
    order_by=lambda: child_name.c.id,
)
PMParent.children_with_parent = relationship(  # type: ignore[attr-defined]
    PMChildWithParent,
    primaryjoin=lambda: PMParent.id == foreign(child_with_parent.c.parent_id),
    viewonly=True,
    order_by=lambda: child_with_parent.c.id,
)
PMChildName.parent = relationship(  # type: ignore[attr-defined]
    PMParent, primaryjoin=lambda: PMParent.id == foreign(child_name.c.parent_id), viewonly=True
)
