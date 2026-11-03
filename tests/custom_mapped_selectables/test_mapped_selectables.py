"""A class whose mapped selectable is a subquery is filtered on that subquery's soft-delete column, and
the mapping itself is never modified."""

from sqlalchemy import func, select

from tests.custom_mapped_selectables.model import MSEmployee, MSItemName, MSItemView, MSManager


def _names(session, stmt):
    return sorted(row.name for row in session.scalars(stmt))


def test_polymorphic_union_of_a_concrete_hierarchy_is_filtered(db_session):
    assert _names(db_session, select(MSEmployee)) == ["employee", "manager"]


def test_concrete_subclass_is_filtered(db_session):
    assert _names(db_session, select(MSManager)) == ["manager"]


def test_count_over_a_concrete_hierarchy_is_filtered(db_session):
    assert db_session.scalar(select(func.count()).select_from(MSEmployee)) == 2


def test_class_mapped_to_a_select_is_filtered(db_session):
    assert _names(db_session, select(MSItemView)) == ["item"]


def test_count_of_a_class_mapped_to_a_select_is_filtered(db_session):
    assert db_session.scalar(select(func.count()).select_from(MSItemView)) == 1


def test_include_deleted_after_filtered_queries_includes_everything(db_session):
    """The mapping is not modified by the filtered queries before."""
    for _ in range(2):
        assert _names(db_session, select(MSEmployee)) == ["employee", "manager"]
        assert _names(db_session, select(MSItemView)) == ["item"]

    everyone = _names(db_session, select(MSEmployee).execution_options(include_deleted=True))
    items = _names(db_session, select(MSItemView).execution_options(include_deleted=True))

    assert everyone == ["employee", "erased employee", "erased manager", "manager"]
    assert items == ["erased item", "item"]


def test_class_mapped_to_a_select_without_the_soft_delete_column_is_filtered(db_session):
    """The SELECT the class is mapped to is filtered inside, without modifying the mapping."""
    assert _names(db_session, select(MSItemName)) == ["item"]
    assert _names(db_session, select(MSItemName).execution_options(include_deleted=True)) == ["erased item", "item"]
    assert _names(db_session, select(MSItemName)) == ["item"]
