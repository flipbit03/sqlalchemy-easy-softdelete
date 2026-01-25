"""Tests for integer field type option."""

from datetime import datetime, timezone

from sqlalchemy import Integer

from tests.integer_field_type.model import IFTTable


def test_field_is_integer_type():
    """Verify the field uses Integer type."""
    column = IFTTable.__table__.columns["deleted_at"]
    assert isinstance(column.type, Integer)


def test_delete_sets_integer_timestamp(db_session):
    """Verify delete() sets an integer timestamp."""
    obj = IFTTable(value=1)
    db_session.add(obj)
    db_session.commit()

    before = int(datetime.now(timezone.utc).timestamp())
    obj.delete()
    after = int(datetime.now(timezone.utc).timestamp())

    assert isinstance(obj.deleted_at, int)
    assert before <= obj.deleted_at <= after


def test_undelete_clears_integer_field(db_session):
    """Verify undelete() clears the integer field."""
    obj = IFTTable(value=1)
    db_session.add(obj)
    db_session.commit()

    obj.delete()
    assert obj.deleted_at is not None

    obj.undelete()
    assert obj.deleted_at is None


def test_integer_field_soft_delete_filtering(db_session):
    """Verify soft-delete filtering works with integer field."""
    active = IFTTable(value=1)
    deleted = IFTTable(value=2)
    deleted.deleted_at = int(datetime.now(timezone.utc).timestamp())

    db_session.add_all([active, deleted])
    db_session.commit()

    results = db_session.query(IFTTable).all()
    assert len(results) == 1
    assert results[0].value == 1
