"""Tests for `sqlalchemy_easy_softdelete` package."""

from tests.model import SDChild, SDParent


def test_ensure_stable_seed_data(snapshot, seeded_session):
    all_parents = seeded_session.query(SDParent).execution_options(include_deleted=True).all()
    all_children = seeded_session.query(SDChild).execution_options(include_deleted=True).all()

    snapshot.assert_match(all_parents)
    snapshot.assert_match(all_children)
