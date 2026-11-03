"""The rewrite cache: statements of the same shape reuse the analysis of their FROM clause.

A cached rewrite must give the same SQL as an uncached one, for every kind of statement, and a
statement must never be served a plan that does not hold for it.
"""

import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import Integer, column, func, lambda_stmt, select, text, true, union, values
from sqlalchemy.orm import aliased, joinedload, selectinload, subqueryload
from sqlalchemy.sql import Select

from sqlalchemy_easy_softdelete.handler.rewriter import DEFAULT_REWRITE_CACHE_SIZE, SoftDeleteQueryRewriter
from tests.default_config.model import SDChild, SDChildChild, SDParent, SDSimpleTable, SoftDeleteMixin


def _rewriter(rewrite_cache_size: int = DEFAULT_REWRITE_CACHE_SIZE) -> SoftDeleteQueryRewriter:
    live = SoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter
    return SoftDeleteQueryRewriter(
        live.deleted_field_name,
        live.disable_soft_delete_option_name,
        live.ignored_tables,
        rewrite_cache_size=rewrite_cache_size,
    )


def _compiled(stmt: Any) -> tuple[str, dict[str, Any]]:
    if hasattr(stmt, "_resolved"):
        stmt = stmt._resolved
    compiled = stmt.compile()
    return str(compiled), compiled.params


def _count_final_froms() -> Any:
    return patch.object(Select, "get_final_froms", autospec=True, side_effect=Select.get_final_froms)


def _subquery_join() -> Any:
    sq = select(SDChild.parent_id).subquery()
    return select(SDParent.id).join(sq, sq.c.parent_id == SDParent.id)


def _aliased_subquery() -> Any:
    return select(aliased(SDChild, select(SDChild).subquery()))


STATEMENTS: dict[str, Callable[[], Any]] = {
    "entity": lambda: select(SDChild),
    "where": lambda: select(SDChild).where(SDChild.parent_id == 1000),
    "join": lambda: select(SDChild).join(SDChild.parent),
    "join_chain": lambda: select(SDChildChild).join(SDChildChild.child).join(SDChild.parent),
    "outerjoin": lambda: select(SDChild).outerjoin(SDChild.parent),
    "count": lambda: select(func.count()).select_from(SDChild),
    "joinedload": lambda: select(SDParent).options(joinedload(SDParent.children)),
    "joinedload_limit": lambda: select(SDParent).options(joinedload(SDParent.children)).limit(5),
    "selectinload": lambda: select(SDParent).options(selectinload(SDParent.children)),
    "subqueryload": lambda: select(SDParent).options(subqueryload(SDParent.children)),
    "subquery_join": _subquery_join,
    "subquery_columns": lambda: (lambda sq: select(sq.c.id))(select(SDChild.id).subquery()),
    "union_subquery": lambda: (lambda sq: select(sq.c.id))(union(select(SDChild.id), select(SDParent.id)).subquery()),
    "aliased_subquery": _aliased_subquery,
    "union": lambda: union(select(SDChild.id), select(SDChildChild.id)),
    "from_statement": lambda: select(SDChild).from_statement(select(SDChild)),
    "lambda": lambda: lambda_stmt(lambda: select(SDChild).join(SDParent)),
    "text_from": lambda: select(text("id")).select_from(text("sdchild")),
    "include_deleted": lambda: select(SDChild).execution_options(include_deleted=True),
}


@pytest.mark.parametrize("make", STATEMENTS.values(), ids=STATEMENTS.keys())
def test_cached_rewrite_gives_the_same_sql_as_an_uncached_one(make):
    cached, uncached = _rewriter(), _rewriter(rewrite_cache_size=0)
    expected = _compiled(uncached.rewrite_statement(make()))

    assert _compiled(cached.rewrite_statement(make())) == expected
    assert _compiled(cached.rewrite_statement(make())) == expected


def test_a_repeated_statement_shape_is_analyzed_once():
    rewriter = _rewriter()

    with _count_final_froms() as final_froms:
        for _ in range(3):
            rewriter.rewrite_statement(select(SDChild).join(SDChild.parent))

    assert final_froms.call_count == 1
    info = rewriter.rewrite_cache_info()
    assert (info.hits, info.misses, info.uncacheable, info.size) == (2, 1, 0, 1)


def test_statements_differing_only_in_parameter_values_share_a_plan():
    rewriter = _rewriter()

    with _count_final_froms() as final_froms:
        for parent_id in (1000, 1001, 1002):
            rewriter.rewrite_statement(select(SDChild).where(SDChild.parent_id == parent_id))

    assert final_froms.call_count == 1


def test_a_statement_holding_a_subquery_reuses_its_plan():
    """Statements of this shape stop being analyzed after the first ones.

    On SQLAlchemy 2.0, analyzing the outer statement leaves ORM compile options on the statement
    inside the subquery, which are part of that statement's cache key. The next statement of the
    shape, whose outer statement is not analyzed, analyzes its inner statement once more.
    """
    rewriter = _rewriter()
    first_sql = {_compiled(rewriter.rewrite_statement(_subquery_join()))[0] for _ in range(2)}

    with _count_final_froms() as final_froms:
        sql = {_compiled(rewriter.rewrite_statement(_subquery_join()))[0] for _ in range(3)}

    assert final_froms.call_count == 0
    assert sql == first_sql
    assert len(sql) == 1


def test_a_statement_selecting_from_an_aliased_subquery_reuses_its_plan():
    """Like a subquery the statement holds, the subquery of an aliased() entity is found again without
    analyzing the statement."""
    rewriter = _rewriter()
    first_sql = {_compiled(rewriter.rewrite_statement(_aliased_subquery()))[0] for _ in range(2)}

    with _count_final_froms() as final_froms:
        sql = {_compiled(rewriter.rewrite_statement(_aliased_subquery()))[0] for _ in range(3)}

    assert final_froms.call_count == 0
    assert sql == first_sql
    assert len(sql) == 1
    assert rewriter.rewrite_cache_info().uncacheable == 0


def _active(entity: Any, disabled: bool = False) -> Any:
    return true() if disabled else entity.deleted_at.is_(None)


# Each case: (builds the statement, whose nested statement includes soft-deleted rows when `disabled`;
# builds the expected rows' statement, filtered explicitly)
TWINS: dict[str, tuple[Callable[[bool], Any], Callable[[bool], Any]]] = {
    "held_subquery": (
        lambda disabled: (lambda sq: select(SDParent.id).join(sq, sq.c.parent_id == SDParent.id))(
            select(SDChild.parent_id).execution_options(include_deleted=disabled).subquery()
        ),
        lambda disabled: (
            select(SDParent.id)
            .join(SDChild, SDChild.parent_id == SDParent.id)
            .where(_active(SDParent), _active(SDChild, disabled))
        ),
    ),
    "aliased_subquery": (
        lambda disabled: select(
            aliased(SDChild, select(SDChild).execution_options(include_deleted=disabled).subquery()).id
        ),
        lambda disabled: select(SDChild.id).where(_active(SDChild, disabled)),
    ),
    "union_member": (
        lambda disabled: (lambda sq: select(sq.c.id))(
            union(select(SDChild.id), select(SDChildChild.id).execution_options(include_deleted=disabled)).subquery()
        ),
        lambda disabled: union(
            select(SDChild.id).where(_active(SDChild)), select(SDChildChild.id).where(_active(SDChildChild, disabled))
        ),
    ),
}


@pytest.mark.parametrize("make, make_expected", TWINS.values(), ids=TWINS.keys())
def test_include_deleted_inside_a_subquery_is_honored_by_same_shape_statements(seeded_session, make, make_expected):
    """Execution options are not part of a cache key: both statements have the same one."""
    assert make(True)._generate_cache_key().key == make(False)._generate_cache_key().key
    uncached = _rewriter(rewrite_cache_size=0)
    expected_sql = {disabled: _compiled(uncached.rewrite_statement(make(disabled))) for disabled in (False, True)}
    assert expected_sql[False] != expected_sql[True]

    cached = _rewriter()
    for disabled in (False, True, False, True):
        assert _compiled(cached.rewrite_statement(make(disabled))) == expected_sql[disabled]
        # Through the session's hook as well, against the database.
        rows = sorted(seeded_session.execute(make(disabled)).all())
        expected = sorted(seeded_session.execute(make_expected(disabled).execution_options(include_deleted=True)).all())
        assert rows == expected


def test_least_recently_used_shape_is_evicted():
    rewriter = _rewriter(rewrite_cache_size=2)
    shapes = [STATEMENTS["entity"], STATEMENTS["join"], STATEMENTS["count"]]

    for make in shapes:
        rewriter.rewrite_statement(make())
    with _count_final_froms() as final_froms:
        rewriter.rewrite_statement(shapes[2]())
        rewriter.rewrite_statement(shapes[1]())
        rewriter.rewrite_statement(shapes[0]())

    assert final_froms.call_count == 1
    assert rewriter.rewrite_cache_info().size == 2


def test_zero_cache_size_disables_the_cache():
    rewriter = _rewriter(rewrite_cache_size=0)

    with _count_final_froms() as final_froms:
        for _ in range(3):
            rewriter.rewrite_statement(select(SDChild))

    assert final_froms.call_count == 3
    assert rewriter.rewrite_cache_info() == (0, 0, 0, 0, 0)


def test_negative_cache_size_is_rejected():
    with pytest.raises(ValueError, match="rewrite_cache_size"):
        _rewriter(rewrite_cache_size=-1)


def test_a_statement_without_a_cache_key_is_analyzed_every_time():
    def make() -> Any:
        rows = values(column("id", Integer), name="ids").data([(1,), (2,)])
        return select(SDSimpleTable).join(rows, rows.c.id == SDSimpleTable.id)

    assert make()._generate_cache_key() is None
    rewriter = _rewriter()

    with _count_final_froms() as final_froms:
        for _ in range(2):
            assert "sdsimpletable.deleted_at IS NULL" in _compiled(rewriter.rewrite_statement(make()))[0]

    assert final_froms.call_count == 2
    info = rewriter.rewrite_cache_info()
    assert (info.uncacheable, info.misses, info.size) == (2, 0, 0)


def test_the_default_cache_size():
    assert DEFAULT_REWRITE_CACHE_SIZE == 500
    assert SoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter.rewrite_cache_info().max_size == 500


def test_concurrent_rewrites_under_constant_eviction_match_uncached_rewrites():
    uncached = _rewriter(rewrite_cache_size=0)
    expected = {name: _compiled(uncached.rewrite_statement(make())) for name, make in STATEMENTS.items()}
    cached = _rewriter(rewrite_cache_size=3)
    mismatches: list[str] = []

    def work() -> None:
        for _ in range(20):
            for name, make in STATEMENTS.items():
                if _compiled(cached.rewrite_statement(make())) != expected[name]:
                    mismatches.append(name)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert mismatches == []
    assert cached.rewrite_cache_info().size <= 3
