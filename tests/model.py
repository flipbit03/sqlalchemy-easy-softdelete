from datetime import datetime
from typing import List

from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import as_declarative, declared_attr, relationship

from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


@as_declarative()
class TestModelBase:
    @declared_attr
    def __tablename__(cls) -> str:
        return cls.__name__.lower()

    id = Column(Integer, primary_key=True)

    def __repr__(self):
        return f"<{self.__class__.__name__} id={self.id}>"


class SoftDeleteMixin(generate_soft_delete_mixin_class()):
    # for autocomplete
    deleted_at: datetime


class SDParent(TestModelBase, SoftDeleteMixin):
    children: 'List[SDChild]' = relationship('SDChild')

    def __repr__(self):
        return f"<{self.__class__.__name__} id={self.id} deleted={bool(self.deleted_at)}>"


class SDChild(TestModelBase, SoftDeleteMixin):
    parent_id = Column(Integer, ForeignKey(f'{SDParent.__tablename__}.id'), nullable=False)
    parent: SDParent = relationship('SDParent', back_populates="children")

    def __repr__(self):
        pid = f"(parent_id={self.parent_id})"
        left = f"{self.__class__.__name__} id={self.id} deleted={bool(self.deleted_at)}"
        return f"<{left:30} {pid:>15}>"


class SDBaseRequest(
    TestModelBase,
    SoftDeleteMixin,
):
    request_type = Column(String(50))

    __mapper_args__ = {
        "polymorphic_identity": "sdbaserequest",
        "polymorphic_on": request_type,
    }


class SDDerivedRequest(SDBaseRequest):
    id: Integer = Column(Integer, ForeignKey("sdbaserequest.id"), primary_key=True)

    __mapper_args__ = {
        "polymorphic_identity": "sdderivedrequest",
    }
