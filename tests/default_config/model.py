from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, as_declarative, relationship

from sqlalchemy_easy_softdelete.hook import IgnoredTable
from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class TestModelBase:
    id = Column(Integer, primary_key=True, autoincrement=True)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.id}>"


class SoftDeleteMixin(
    generate_soft_delete_mixin_class(  # type: ignore[misc]
        ignored_tables=[
            IgnoredTable(table_schema=None, name="sdtablethatshouldnotbesoftdeleted"),
        ],
    )
):
    # Type hint for IDE autocomplete and type checker support.
    # Using Mapped[T | None] ensures type checkers understand this is a
    # SQLAlchemy column that supports query operations like .where()
    deleted_at: Mapped[datetime | None]

    # Optional: Add method stubs for delete/undelete for type checker support.
    # The actual implementations are provided by the generated mixin class.
    def delete(self, v: datetime | None = None) -> None:
        super().delete(v)  # type: ignore[misc]

    def undelete(self) -> None:
        super().undelete()  # type: ignore[misc]


class SDSimpleTable(TestModelBase, SoftDeleteMixin):
    __tablename__ = "sdsimpletable"
    int_field = Column(Integer)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.id} deleted={bool(self.deleted_at)}>"


class SDParent(TestModelBase, SoftDeleteMixin):
    __tablename__ = "sdparent"
    children: Mapped[list["SDChild"]] = relationship("SDChild", back_populates="parent")  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.id} deleted={bool(self.deleted_at)}>"


class SDChild(TestModelBase, SoftDeleteMixin):
    __tablename__ = "sdchild"
    parent_id = Column(Integer, ForeignKey("sdparent.id"), nullable=False)
    parent: Mapped["SDParent"] = relationship("SDParent", back_populates="children")  # type: ignore[assignment]
    child_children: Mapped[list["SDChildChild"]] = relationship("SDChildChild", back_populates="child")  # type: ignore[assignment]

    def __repr__(self) -> str:
        pid = f"(parent_id={self.parent_id})"
        left = f"{self.__class__.__name__} id={self.id} deleted={bool(self.deleted_at)}"
        return f"<{left:30} {pid:>15}>"


class SDChildChild(TestModelBase, SoftDeleteMixin):
    __tablename__ = "sdchildchild"
    child_id = Column(Integer, ForeignKey("sdchild.id"), nullable=False)
    child: Mapped["SDChild"] = relationship("SDChild", back_populates="child_children")  # type: ignore[assignment]

    def __repr__(self) -> str:
        pid = f"(child_id={self.child_id})"
        left = f"{self.__class__.__name__} id={self.id} deleted={bool(self.deleted_at)}"
        return f"<{left:30} {pid:>15}>"


class SDBaseRequest(TestModelBase, SoftDeleteMixin):
    __tablename__ = "sdbaserequest"
    request_type = Column(String(50))
    base_field = Column(Integer)

    __mapper_args__ = {
        "polymorphic_identity": "sdbaserequest",
        "polymorphic_on": "request_type",
    }


class SDDerivedRequest(SDBaseRequest):
    __tablename__ = "sdderivedrequest"
    id = Column(Integer, ForeignKey("sdbaserequest.id"), primary_key=True)
    derived_field = Column(Integer)

    __mapper_args__ = {
        "polymorphic_identity": "sdderivedrequest",
    }


class SDTableThatShouldNotBeSoftDeleted(TestModelBase):
    __tablename__ = "sdtablethatshouldnotbesoftdeleted"
    id = Column(Integer, primary_key=True)
    deleted_at = Column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.id}>"
