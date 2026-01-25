"""Tests for custom default value option."""

from datetime import datetime, timezone

from tests.custom_default_value.model import CDVTable


def test_delete_uses_custom_default_value(db_session):
    """Verify delete() uses the custom default value function."""
    obj = CDVTable(value=1)
    db_session.add(obj)
    db_session.commit()

    obj.delete()

    # Should use our custom date (2000-01-01)
    # SQLite doesn't preserve timezone, so compare without it
    assert obj.deleted_at.replace(tzinfo=None) == datetime(2000, 1, 1)


def test_delete_with_explicit_value_overrides_default(db_session):
    """Verify delete(value) uses the passed value instead of default."""
    obj = CDVTable(value=1)
    db_session.add(obj)
    db_session.commit()

    custom_date = datetime(2020, 6, 15, 12, 30, tzinfo=timezone.utc)
    obj.delete(custom_date)

    # SQLite doesn't preserve timezone, so compare without it
    assert obj.deleted_at.replace(tzinfo=None) == custom_date.replace(tzinfo=None)
