"""Tests for custom method names option."""

from tests.custom_method_names.model import CMNSoftDeleteMixin, CMNTable


def test_custom_method_names_exist():
    """Verify custom method names are used."""
    assert hasattr(CMNSoftDeleteMixin, "soft_delete")
    assert hasattr(CMNSoftDeleteMixin, "restore")
    # Original names should not exist on the generated parent class
    generated_class = CMNSoftDeleteMixin.__bases__[0]
    assert not hasattr(generated_class, "delete")
    assert not hasattr(generated_class, "undelete")


def test_soft_delete_method_sets_deleted_at(db_session):
    """Verify soft_delete() method works."""
    obj = CMNTable(value=1)
    db_session.add(obj)
    db_session.commit()

    assert obj.deleted_at is None
    obj.soft_delete()
    assert obj.deleted_at is not None


def test_restore_method_clears_deleted_at(db_session):
    """Verify restore() method works."""
    obj = CMNTable(value=1)
    db_session.add(obj)
    db_session.commit()

    obj.soft_delete()
    assert obj.deleted_at is not None

    obj.restore()
    assert obj.deleted_at is None
