"""A subquery of textual SQL (``text(...).columns(...).subquery()``) is left as it is, like raw SQL anywhere else:
the library cannot filter raw SQL. The rest of the statement is filtered as usual."""

from sqlalchemy import select, text, union_all
from sqlalchemy.orm import Session, aliased, selectinload

from tests.default_config.model import SDChild, SDParent

RAW_CHILDREN = text("SELECT id, parent_id, deleted_at FROM sdchild")


def _raw_subquery():
    return RAW_CHILDREN.columns(SDChild.id, SDChild.parent_id, SDChild.deleted_at).subquery("raw_children")


def _all_child_ids(session: Session) -> list[int]:
    return sorted(session.scalars(select(SDChild.id).execution_options(include_deleted=True)))


def test_select_from_a_textual_subquery_returns_its_rows(seeded_session):
    raw = _raw_subquery()

    ids = seeded_session.scalars(select(raw.c.id)).all()

    assert sorted(ids) == _all_child_ids(seeded_session)


def test_aliased_entity_over_a_textual_subquery_returns_its_rows(seeded_session):
    seeded_session.expunge_all()
    children = aliased(SDChild, _raw_subquery())

    loaded = seeded_session.scalars(select(children)).all()

    assert sorted(child.id for child in loaded) == _all_child_ids(seeded_session)


def test_join_to_a_textual_subquery_filters_the_rest_of_the_statement(seeded_session):
    raw = _raw_subquery()
    stmt = select(SDParent.id, raw.c.id).join(raw, raw.c.parent_id == SDParent.id)

    rows = seeded_session.execute(stmt).all()

    expected = seeded_session.execute(
        select(SDParent.id, SDChild.id)
        .join(SDChild, SDChild.parent_id == SDParent.id)
        .where(SDParent.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    ).all()
    assert sorted(rows) == sorted(expected)


def test_union_member_selecting_from_a_textual_subquery(seeded_session):
    raw = _raw_subquery()
    stmt = union_all(
        select(SDChild.id).where(SDChild.parent_id == 1000), select(raw.c.id).where(raw.c.parent_id == 1001)
    )

    ids = seeded_session.scalars(stmt).all()

    expected = seeded_session.scalars(
        select(SDChild.id)
        .where(
            ((SDChild.parent_id == 1000) & SDChild.deleted_at.is_(None)) | (SDChild.parent_id == 1001),
        )
        .execution_options(include_deleted=True)
    ).all()
    assert sorted(ids) == sorted(expected)


def test_eager_loading_elsewhere_in_the_statement_leaves_a_textual_alias_alone(seeded_session):
    seeded_session.expunge_all()
    children = aliased(SDChild, _raw_subquery())
    stmt = (
        select(children.id)
        .join(SDParent, SDParent.id == children.parent_id)
        .add_columns(SDParent)
        .options(selectinload(SDParent.children))
    )

    ids = [row[0] for row in seeded_session.execute(stmt)]

    expected = seeded_session.scalars(
        select(SDChild.id)
        .join(SDParent, SDParent.id == SDChild.parent_id)
        .where(SDParent.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    ).all()
    assert sorted(ids) == sorted(expected)


def test_selectin_load_into_a_textual_alias_without_the_soft_delete_column(seeded_session):
    """Eager loading criteria cannot filter an alias without the soft-delete column: filtering the class's
    table instead would add it to the FROM clause, one row per row of the table."""
    seeded_session.expunge_all()
    raw = text("SELECT id, parent_id FROM sdchild").columns(SDChild.id, SDChild.parent_id).subquery("raw_ids")
    children = aliased(SDChild, raw)

    parents = seeded_session.scalars(select(SDParent).options(selectinload(SDParent.children.of_type(children))))
    loaded = {parent.id: sorted(child.id for child in parent.children) for parent in parents}

    expected: dict[int, list[int]] = {}
    for parent_id, child_id in seeded_session.execute(
        select(SDChild.parent_id, SDChild.id)
        .join(SDChild.parent)
        .where(SDParent.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    ):
        expected.setdefault(parent_id, []).append(child_id)
    assert loaded == {parent_id: sorted(child_ids) for parent_id, child_ids in expected.items()}
