"""Rows of rewritten statements stay keyed by the caller's objects: ``row._mapping[...]`` finds the
columns of a subquery, and an ``aliased()`` entity and its columns.

The statements are built once and executed twice, the second time with the remembered rewrite.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from tests.default_config.model import SDChild, SDParent

CHILDREN = select(SDChild).subquery()
COUNTS = select(SDChild.parent_id, func.count().label("n")).group_by(SDChild.parent_id).subquery()
ALIASED_CHILDREN = aliased(SDChild, select(SDChild).subquery())
LAST_CHILDREN = aliased(SDChild, select(SDChild).order_by(SDChild.id.desc()).limit(3).subquery())


def _active_child_ids(session: Session) -> list[int]:
    stmt = select(SDChild.id).where(SDChild.deleted_at.is_(None)).execution_options(include_deleted=True)
    return sorted(session.scalars(stmt))


def test_row_keyed_by_a_subquery_column(seeded_session):
    stmt = select(CHILDREN.c.id, CHILDREN.c.parent_id)

    for _ in range(2):
        ids = sorted(row._mapping[CHILDREN.c.id] for row in seeded_session.execute(stmt))

        assert ids == _active_child_ids(seeded_session)


def test_row_keyed_by_a_column_of_a_subquery_built_for_each_statement(seeded_session):
    for _ in range(2):
        children = select(SDChild).subquery()

        ids = sorted(row._mapping[children.c.id] for row in seeded_session.execute(select(children.c.id)))

        assert ids == _active_child_ids(seeded_session)


def test_row_keyed_by_a_joined_subquery_column(seeded_session):
    stmt = select(SDParent.id, COUNTS.c.n).join(COUNTS, COUNTS.c.parent_id == SDParent.id)
    expected = dict(
        seeded_session.execute(
            select(SDChild.parent_id, func.count())
            .join(SDChild.parent)
            .where(SDChild.deleted_at.is_(None), SDParent.deleted_at.is_(None))
            .group_by(SDChild.parent_id)
            .execution_options(include_deleted=True)
        ).all()
    )

    for _ in range(2):
        counts = {row._mapping[SDParent.id]: row._mapping[COUNTS.c.n] for row in seeded_session.execute(stmt)}

        assert counts == expected


def test_row_keyed_by_an_aliased_subquery_entity(seeded_session):
    for _ in range(2):
        children = [row._mapping[ALIASED_CHILDREN] for row in seeded_session.execute(select(ALIASED_CHILDREN))]

        assert sorted(child.id for child in children) == _active_child_ids(seeded_session)


def test_row_keyed_by_an_entity_of_an_aliased_subquery_with_a_limit(seeded_session):
    for _ in range(2):
        children = [row._mapping[LAST_CHILDREN] for row in seeded_session.execute(select(LAST_CHILDREN))]

        assert sorted(child.id for child in children) == _active_child_ids(seeded_session)[-3:]


def test_row_keyed_by_an_aliased_subquery_column(seeded_session):
    stmt = select(ALIASED_CHILDREN.id, ALIASED_CHILDREN.parent_id)

    for _ in range(2):
        ids = sorted(row._mapping[ALIASED_CHILDREN.id] for row in seeded_session.execute(stmt))

        assert ids == _active_child_ids(seeded_session)
