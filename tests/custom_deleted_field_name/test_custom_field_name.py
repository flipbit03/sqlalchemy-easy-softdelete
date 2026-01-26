"""Tests for custom deleted_field_name option."""

from datetime import datetime, timezone

from tests.custom_deleted_field_name.model import CFNTable


def test_custom_field_name_column_exists():
    """Verify the column uses the custom field name."""
    assert "removed_at" in CFNTable.__table__.columns
    assert "deleted_at" not in CFNTable.__table__.columns


def test_rewriter_has_correct_field_name(rewriter):
    """Verify the rewriter is configured with the custom field name."""
    assert rewriter.deleted_field_name == "removed_at"


def test_delete_sets_custom_field(db_session):
    """Verify delete() sets the custom field."""
    obj = CFNTable(value=1)
    db_session.add(obj)
    db_session.commit()

    assert obj.removed_at is None
    obj.delete()
    assert obj.removed_at is not None


def test_undelete_clears_custom_field(db_session):
    """Verify undelete() clears the custom field."""
    obj = CFNTable(value=1)
    db_session.add(obj)
    db_session.commit()

    obj.delete()
    assert obj.removed_at is not None

    obj.undelete()
    assert obj.removed_at is None


def test_soft_delete_filtering_uses_custom_field(db_session):
    """Verify soft-delete filtering works with custom field name."""
    active = CFNTable(value=1)
    deleted = CFNTable(value=2)
    deleted.removed_at = datetime.now(timezone.utc)

    db_session.add_all([active, deleted])
    db_session.commit()

    results = db_session.query(CFNTable).all()
    assert len(results) == 1
    assert results[0].value == 1

    all_results = db_session.query(CFNTable).execution_options(include_deleted=True).all()
    assert len(all_results) == 2
