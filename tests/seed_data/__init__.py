from datetime import datetime

from sqlalchemy.orm import Session

from tests.model import SDDerivedRequest


def generate_table_with_inheritance_obj(s: Session, obj_id: int, deleted: bool = False):
    deleted_at = datetime.utcnow() if deleted else None
    new_parent = SDDerivedRequest(id=obj_id, deleted_at=deleted_at)
    s.add(new_parent)
    s.commit()
