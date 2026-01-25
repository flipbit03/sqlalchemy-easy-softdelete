"""Tests for `sqlalchemy_easy_softdelete` package."""

from tests.model import SDChild, SDChildChild, SDParent


def test_ensure_stable_seed_data(snapshot, seeded_session):
    all_parents = seeded_session.query(SDParent).execution_options(include_deleted=True).all()
    all_children = seeded_session.query(SDChild).execution_options(include_deleted=True).all()
    all_child_children = seeded_session.query(SDChildChild).execution_options(include_deleted=True).all()

    assert [repr(p) for p in all_parents] == snapshot
    assert [repr(c) for c in all_children] == snapshot
    assert [repr(cc) for cc in all_child_children] == snapshot
