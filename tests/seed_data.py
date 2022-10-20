import random
from datetime import datetime

from sqlalchemy.orm import Session

from tests.model import SDChild, SDDerivedRequest, SDParent


def generate_parent_child_object_hierarchy(
    s: Session, parent_id: int, min_children: int = 1, max_children: int = 5, parent_deleted: bool = False
):
    # Fix a seed in the RNG for deterministic outputs
    random.seed(parent_id)

    # Generate the Parent
    deleted_at = datetime.utcnow() if parent_deleted else None
    new_parent = SDParent(id=parent_id, deleted_at=deleted_at)
    s.add(new_parent)
    s.flush()

    active_children = random.randint(min_children, max_children)

    # Add some active children
    for active_id in range(active_children):
        new_child = SDChild(id=parent_id * 1000 + active_id, parent=new_parent)
        s.add(new_child)
        s.flush()

    # Add some soft-deleted children
    for inactive_id in range(random.randint(min_children, max_children)):
        new_soft_deleted_child = SDChild(
            id=parent_id * 1000 + active_children + inactive_id,
            parent=new_parent,
            deleted_at=datetime.utcnow(),
        )
        s.add(new_soft_deleted_child)
        s.flush()

    s.commit()


def generate_table_with_inheritance_obj(s: Session, obj_id: int, deleted: bool = False):
    deleted_at = datetime.utcnow() if deleted else None
    new_parent = SDDerivedRequest(id=obj_id, deleted_at=deleted_at)
    s.add(new_parent)
    s.commit()
