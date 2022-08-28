"""Tests for `sqlalchemy_easy_softdelete` package."""

from tests.model import SDChild, SDParent


def test_query_single_table(snapshot, seeded_session):
    all_active_children = seeded_session.query(SDChild).all()

    snapshot.assert_match(all_active_children)


def test_ensure_multiple_table_deletion_works(snapshot, seeded_session):
    all_active_children = seeded_session.query(SDChild).join(SDParent).all()

    snapshot.assert_match(all_active_children)


def test_ensure_aggregate_from_multiple_table_deletion_works_active_object_count(snapshot, seeded_session):
    all_active_children = seeded_session.query(SDChild).join(SDParent).count()

    snapshot.assert_match(all_active_children)
