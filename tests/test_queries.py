"""Tests for `sqlalchemy_easy_softdelete` package."""

from tests.model import SDChild, SDDerivedRequest, SDParent


def test_query_single_table(snapshot, seeded_session):
    all_active_children = seeded_session.query(SDChild).all()

    snapshot.assert_match(all_active_children)


def test_ensure_multiple_table_deletion_works(snapshot, seeded_session):
    all_active_children = seeded_session.query(SDChild).join(SDParent).all()

    snapshot.assert_match(all_active_children)


def test_ensure_aggregate_from_multiple_table_deletion_works_active_object_count(snapshot, seeded_session):
    all_active_children = seeded_session.query(SDChild).join(SDParent).count()

    snapshot.assert_match(all_active_children)


def test_ensure_table_with_inheritance_works(snapshot, seeded_session):
    all_active_derived_requests = seeded_session.query(SDDerivedRequest).all()

    assert len(all_active_derived_requests) == 2
    snapshot.assert_match(all_active_derived_requests)

    all_active_and_deleted_derived_requests = (
        seeded_session.query(SDDerivedRequest).execution_options(include_deleted=True).all()
    )

    assert len(all_active_and_deleted_derived_requests) == 3
    snapshot.assert_match(all_active_and_deleted_derived_requests)
