from sqlalchemy import select
from sqlalchemy.orm import joinedload

from tests.custom_rewrite_cache_size.model import CRCChild, CRCParent
from tests.default_config.model import SDParent, SoftDeleteMixin


def test_the_configured_cache_size_is_used(rewriter):
    assert rewriter.rewrite_cache_info().max_size == 2000


def test_repeated_queries_reuse_the_rewrite(rewriter):
    hits_before = rewriter.rewrite_cache_info().hits

    for _ in range(3):
        rewriter.rewrite_statement(select(CRCChild).join(CRCChild.parent))

    assert rewriter.rewrite_cache_info().hits >= hits_before + 2


def test_eager_load_is_filtered_with_the_configured_field(db_session):
    db_session.add(CRCParent(id=1))
    db_session.flush()
    kept, archived = CRCChild(id=1, parent_id=1), CRCChild(id=2, parent_id=1)
    db_session.add_all([kept, archived])
    db_session.flush()
    archived.delete()
    db_session.flush()

    for _ in range(2):
        db_session.expire_all()
        parent = db_session.scalars(select(CRCParent).options(joinedload(CRCParent.children))).unique().one()
        assert [child.id for child in parent.children] == [1]


def test_two_configurations_filter_eager_loads_with_their_own_field(rewriter):
    """Each configuration's loader criteria name its own field, however the two alternate."""
    default_rewriter = SoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter

    for _ in range(2):
        custom_sql = str(rewriter.rewrite_statement(select(CRCParent).options(joinedload(CRCParent.children))))
        default_sql = str(default_rewriter.rewrite_statement(select(SDParent).options(joinedload(SDParent.children))))
        assert "crcchild_1.archived_at IS NULL" in custom_sql
        assert "sdchild_1.deleted_at IS NULL" in default_sql
        assert "deleted_at" not in custom_sql
        assert "archived_at" not in default_sql
