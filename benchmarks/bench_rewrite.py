"""Microseconds per statement for the rewriter, by statement shape: prints one JSON object.

    python benchmarks/bench_rewrite.py [--repeat 5]

For each shape, ``warm`` is a rewrite with the plan cache (statements of a shape it has seen), ``cold`` one
without it (``rewrite_cache_size=0``: every statement is planned), and ``execute`` a full ``session.execute()`` on
SQLite, the rewrite included. Each figure is the best of ``--repeat`` runs. ``ab.py`` compares two versions.
"""

from __future__ import annotations

import argparse
import json
import timeit
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, create_engine, func, lambda_stmt, or_, select, true
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    column_property,
    contains_eager,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
)

from sqlalchemy_easy_softdelete.handler.rewriter import SoftDeleteQueryRewriter
from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class


class BenchBase(DeclarativeBase):
    pass


class BenchSoftDelete(generate_soft_delete_mixin_class()):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]


class BParent(BenchBase, BenchSoftDelete):
    __tablename__ = "b_parent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(50))
    children: Mapped[list[BChild]] = relationship(back_populates="parent", order_by="BChild.id")
    owners: Mapped[list[BOwner]] = relationship(back_populates="parent", order_by="BOwner.id")


class BChild(BenchBase, BenchSoftDelete):
    __tablename__ = "b_child"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("b_parent.id"))
    parent: Mapped[BParent] = relationship(back_populates="children")


class BOwner(BenchBase, BenchSoftDelete):
    __tablename__ = "b_owner"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer, ForeignKey("b_parent.id"))
    parent: Mapped[BParent] = relationship(back_populates="owners")


class BCounted(BenchBase, BenchSoftDelete):
    """A class with a column_property() that counts the active rows of another class."""

    __tablename__ = "b_counted"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


BCounted.child_count = column_property(  # type: ignore[attr-defined]
    select(func.count(BChild.id)).where(BChild.parent_id == BCounted.id).correlate_except(BChild).scalar_subquery()
)


def _count() -> Any:
    return select(func.count(BChild.id)).where(BChild.parent_id == BParent.id).scalar_subquery()


def _tree() -> Any:
    # The recursion ends: each step goes to a higher id (child 1's parent id is 1, its own id).
    tree = select(BChild.id, BChild.parent_id).where(BChild.parent_id == 1).cte("tree", recursive=True)
    return select(
        tree.union_all(
            select(BChild.id, BChild.parent_id).join(tree, (BChild.parent_id == tree.c.id) & (BChild.id > tree.c.id))
        ).c.id
    )


SHAPES: dict[str, Callable[[], Any]] = {
    "simple": lambda: select(BParent).where(BParent.id == 1),
    "column_property_entity": lambda: select(BCounted).where(BCounted.id == 1),
    "joinedload": lambda: select(BParent).options(joinedload(BParent.children)).where(BParent.id == 1),
    "joinedload_or200": lambda: (
        select(BParent).options(joinedload(BParent.children)).where(or_(*[BParent.id == i for i in range(200)]))
    ),
    "selectinload": lambda: select(BParent).options(selectinload(BParent.children)).where(BParent.id == 1),
    "selectinload_and20": lambda: (
        select(BParent).options(selectinload(BParent.children)).where(*[BParent.name != str(i) for i in range(20)])
    ),
    "contains_eager": lambda: select(BParent).join(BParent.children).options(contains_eager(BParent.children)),
    "any": lambda: select(BParent).where(BParent.children.any()),
    "has_any_x2": lambda: select(BChild).where(
        BChild.parent.has(BParent.owners.any()), BChild.parent.has(BParent.children.any())
    ),
    "scalar_in_columns": lambda: select(BParent.id, _count()),
    "subquery_from": lambda: (lambda sq: select(sq.c.id).where(sq.c.id > 0))(select(BChild).subquery()),
    "aliased_subquery": lambda: select(aliased(BChild, select(BChild).subquery())),
    "cte": lambda: (lambda c: select(c.c.id))(select(BChild.id).cte("c")),
    "recursive_cte": _tree,
    "union": lambda: select(BChild.id).union(select(BChild.id).where(BChild.id > 1)),
    "outer_join": lambda: select(BParent.id, BChild.id).outerjoin(BParent.children),
    "lateral": lambda: (lambda lat: select(BParent.id, lat.c.id).join(lat, true()))(
        select(BChild.id).where(BChild.parent_id == BParent.id).lateral()
    ),
    "lambda_stmt": lambda: lambda_stmt(lambda: select(BParent).where(BParent.id == 1)),
}

# A lateral join renders on PostgreSQL only.
EXECUTED = [name for name in SHAPES if name != "lateral"]


def _best(function: Callable[[], Any], number: int, repeat: int) -> float:
    return min(timeit.repeat(function, number=number, repeat=repeat)) / number * 1e6


def measure(repeat: int, only: list[str] | None = None) -> dict[str, dict[str, float]]:
    engine = create_engine("sqlite://")
    BenchBase.metadata.create_all(engine)
    deleted = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add_all([BParent(id=1), BParent(id=2, deleted_at=deleted)])
        session.flush()
        session.add_all([BChild(id=1, parent_id=1), BChild(id=2, parent_id=1, deleted_at=deleted)])
        session.add_all([BOwner(id=1, parent_id=1), BCounted(id=1)])
        session.commit()
    warm_rewriter: SoftDeleteQueryRewriter = BenchSoftDelete._sqlalchemy_easy_softdelete_rewriter
    cold_rewriter = SoftDeleteQueryRewriter("deleted_at", "include_deleted", [], rewrite_cache_size=0)
    results: dict[str, dict[str, float]] = {}
    session = Session(engine)
    for name, make in SHAPES.items():
        if only and name not in only:
            continue
        statements = [make() for _ in range(50)]
        figures: dict[str, float] = {}
        for label, rewriter in (("warm", warm_rewriter), ("cold", cold_rewriter)):
            for statement in statements[:3]:
                rewriter.rewrite_statement(statement)

            def rewrite_all(rewriter: SoftDeleteQueryRewriter = rewriter, statements: list[Any] = statements) -> None:
                for statement in statements:
                    rewriter.rewrite_statement(statement)

            figures[label] = _best(rewrite_all, 3, repeat) / len(statements)
        if name in EXECUTED:
            for statement in statements[:3]:
                session.execute(statement).unique().all()

            def execute_all(statements: list[Any] = statements) -> None:
                for statement in statements:
                    session.execute(statement).unique().all()

            figures["execute"] = _best(execute_all, 1, repeat) / len(statements)
        results[name] = figures
    session.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--only", nargs="*", help="shapes to measure (default: all)")
    args = parser.parse_args()
    print(json.dumps(measure(args.repeat, args.only)))


if __name__ == "__main__":
    main()
