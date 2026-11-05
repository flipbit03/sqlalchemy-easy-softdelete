"""Relationships that are eager by their mapping (``lazy="joined"``) leave out soft-deleted rows, as ``joinedload()``
does: SQLAlchemy joins them while it compiles a statement that loads their class, with no loader option."""

import pickle

from sqlalchemy import select
from sqlalchemy.orm import aliased, joinedload, lazyload, selectinload

from tests.mapped_eager_relationships.model import MEHolder, MEInnerParent, MENode, MEParent

ACTIVE_CHILDREN = [(1, [1]), (2, [])]


def _children(parents):  # type: ignore[no-untyped-def]
    return [(parent.id, [child.id for child in parent.children]) for parent in parents]


def test_lazy_joined_relationship(db_session):
    stmt = select(MEParent).order_by(MEParent.id)

    for _ in range(2):
        db_session.expunge_all()
        assert _children(db_session.scalars(stmt).unique()) == ACTIVE_CHILDREN


def test_chain_of_lazy_joined_relationships(db_session):
    parent = db_session.scalars(select(MEParent).where(MEParent.id == 1)).unique().one()

    assert [(child.id, [toy.id for toy in child.toys]) for child in parent.children] == [(1, [1])]


def test_self_referential_lazy_joined_relationship_with_a_join_depth(db_session):
    node = db_session.scalars(select(MENode).where(MENode.id == 1)).unique().one()

    assert [(kid.id, [grandkid.id for grandkid in kid.kids]) for kid in node.kids] == [(2, [4])]


def test_lazy_joined_relationship_with_an_inner_join(db_session):
    """An inner join drops a parent whose children are all soft-deleted, as if it had none."""
    parents = db_session.scalars(select(MEInnerParent).order_by(MEInnerParent.id)).unique()

    assert _children(parents) == [(1, [1])]


def test_lazy_joined_relationship_into_a_class_mapped_to_a_select_without_the_column(db_session):
    parents = db_session.scalars(select(MEParent).order_by(MEParent.id)).unique()

    assert [(parent.id, [name.id for name in parent.child_names]) for parent in parents] == ACTIVE_CHILDREN


def test_lazy_joined_relationship_of_an_aliased_entity(db_session):
    parent = aliased(MEParent)

    assert _children(db_session.scalars(select(parent).order_by(parent.id)).unique()) == ACTIVE_CHILDREN


def test_lazy_joined_relationship_with_a_limit(db_session):
    assert _children(db_session.query(MEParent).order_by(MEParent.id).limit(1)) == [(1, [1])]


def test_lazy_joined_relationship_turned_off_by_a_loader_option(db_session):
    parents = db_session.scalars(select(MEParent).options(lazyload(MEParent.children)).order_by(MEParent.id)).unique()

    assert _children(parents) == ACTIVE_CHILDREN


def test_lazy_joined_relationship_with_include_deleted(db_session):
    stmt = select(MEParent).order_by(MEParent.id).execution_options(include_deleted=True)

    assert _children(db_session.scalars(stmt).unique()) == [(1, [1, 2]), (2, [3])]


def test_lazy_joined_relationship_of_a_lazy_loaded_parent(db_session):
    holder = db_session.get(MEHolder, 1)

    assert [child.id for child in holder.parent.children] == [1]


def test_lazy_joined_relationship_of_an_eager_loaded_parent(db_session):
    for loader in (joinedload, selectinload):
        db_session.expunge_all()
        holder = db_session.scalars(select(MEHolder).options(loader(MEHolder.parent))).unique().one()

        assert [child.id for child in holder.parent.children] == [1]


def test_lazy_joined_relationship_of_pickled_instances(db_session):
    parents = db_session.scalars(select(MEParent).order_by(MEParent.id)).unique()

    assert _children(pickle.loads(pickle.dumps(parent)) for parent in parents) == ACTIVE_CHILDREN
