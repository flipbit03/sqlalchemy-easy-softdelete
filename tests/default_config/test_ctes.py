"""CTEs (``select(...).cte()``) are filtered like subqueries: selected from, joined, as the selectable of an
``aliased()`` entity, through aliases of the CTE, and recursive CTEs, whose references to themselves are left
as they are."""

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from tests.default_config.model import SDChild, SDChildChild, SDParent


def _child_ids(session: Session, *, active_only: bool = True) -> list[int]:
    stmt = select(SDChild.id).execution_options(include_deleted=True)
    if active_only:
        stmt = stmt.where(SDChild.deleted_at.is_(None))
    return sorted(session.scalars(stmt))


def _active_parent_child_pairs(session: Session) -> list[tuple[int, int]]:
    stmt = (
        select(SDParent.id, SDChild.id)
        .join(SDChild, SDChild.parent_id == SDParent.id)
        .where(SDParent.deleted_at.is_(None), SDChild.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    return sorted((parent_id, child_id) for parent_id, child_id in session.execute(stmt))


def test_select_from_a_cte(seeded_session):
    children = select(SDChild).cte("children")

    for _ in range(2):
        assert sorted(seeded_session.scalars(select(children.c.id))) == _child_ids(seeded_session)


def test_join_to_a_cte(seeded_session):
    children = select(SDChild).cte("children")
    stmt = select(SDParent.id, children.c.id).join(children, children.c.parent_id == SDParent.id)

    rows = seeded_session.execute(stmt).all()

    assert sorted((parent_id, child_id) for parent_id, child_id in rows) == _active_parent_child_pairs(seeded_session)


def test_aliased_entity_over_a_cte(seeded_session):
    seeded_session.expunge_all()
    children = aliased(SDChild, select(SDChild).cte("children"))

    loaded = seeded_session.scalars(select(children)).all()

    assert sorted(child.id for child in loaded) == _child_ids(seeded_session)


def test_cte_with_include_deleted(seeded_session):
    children = select(SDChild).execution_options(include_deleted=True).cte("children")

    assert sorted(seeded_session.scalars(select(children.c.id))) == _child_ids(seeded_session, active_only=False)


def test_two_aliases_of_a_cte(seeded_session):
    children = select(SDChild).cte("children")
    left, right = children.alias("left_children"), children.alias("right_children")
    stmt = select(left.c.id, right.c.id).join(
        right, (right.c.parent_id == left.c.parent_id) & (right.c.id != left.c.id)
    )

    pairs = sorted((a, b) for a, b in seeded_session.execute(stmt))

    active = _child_ids(seeded_session)
    all_children = seeded_session.execute(select(SDChild.id, SDChild.parent_id).execution_options(include_deleted=True))
    parent_of = {child_id: parent_id for child_id, parent_id in all_children}
    assert pairs
    assert pairs == sorted((a, b) for a in active for b in active if a != b and parent_of[a] == parent_of[b])


def test_recursive_cte(seeded_session):
    """The children of parent 1000, then their children: each step filters its own table."""
    tree = select(SDChild.id.label("node_id")).where(SDChild.parent_id == 1000).cte("tree", recursive=True)
    tree = tree.union_all(select(SDChildChild.id).join(tree, SDChildChild.child_id == tree.c.node_id))

    for _ in range(2):
        node_ids = sorted(seeded_session.scalars(select(tree.c.node_id)))

        children = seeded_session.scalars(
            select(SDChild.id)
            .where(SDChild.parent_id == 1000, SDChild.deleted_at.is_(None))
            .execution_options(include_deleted=True)
        ).all()
        grandchildren = seeded_session.scalars(
            select(SDChildChild.id)
            .where(SDChildChild.child_id.in_(children), SDChildChild.deleted_at.is_(None))
            .execution_options(include_deleted=True)
        ).all()
        assert node_ids == sorted([*children, *grandchildren])


def test_include_deleted_on_a_statement_selecting_from_a_cte(seeded_session):
    children = select(SDChild).cte("children")

    ids = seeded_session.scalars(select(children.c.id).execution_options(include_deleted=True))

    assert sorted(ids) == _child_ids(seeded_session, active_only=False)
