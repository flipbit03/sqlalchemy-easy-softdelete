"""Tests for disabled methods option."""

from datetime import datetime, timezone

from tests.disabled_methods.model import DMSoftDeleteMixin, DMTable


def test_no_delete_method_on_generated_class():
    """Verify delete/undelete methods are not generated."""
    generated_class = DMSoftDeleteMixin.__bases__[0]
    assert not hasattr(generated_class, "delete")
    assert not hasattr(generated_class, "undelete")


def test_can_manually_set_deleted_at(db_session):
    """Verify we can still manually set deleted_at."""
    obj = DMTable(value=1)
    db_session.add(obj)
    db_session.commit()

    obj_id = obj.id
    assert obj.deleted_at is None

    deleted_time = datetime.now(timezone.utc)
    obj.deleted_at = deleted_time
    db_session.commit()

    # After commit, the object is soft-deleted so query with include_deleted
    result = db_session.query(DMTable).execution_options(include_deleted=True).filter(DMTable.id == obj_id).first()
    assert result is not None
    assert result.deleted_at is not None


def test_soft_delete_filtering_still_works(db_session):
    """Verify soft-delete filtering works even without methods."""
    active = DMTable(value=1)
    deleted = DMTable(value=2)
    deleted.deleted_at = datetime.now(timezone.utc)

    db_session.add_all([active, deleted])
    db_session.commit()

    results = db_session.query(DMTable).all()
    assert len(results) == 1
    assert results[0].value == 1
