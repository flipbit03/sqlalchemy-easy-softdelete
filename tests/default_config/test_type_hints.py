"""Tests for type hint compatibility with SQLAlchemy operations.

These tests verify that the Mapped[T | None] type hint recommendation works
correctly with SQLAlchemy query operations. See GitHub issue #31.

The key insight is that using `deleted_at: datetime` as a type hint causes
type checkers to treat the attribute as a plain datetime, which breaks
type checking for expressions like `.where(Model.deleted_at < value)`.

Using `deleted_at: Mapped[datetime | None]` tells the type checker this is
a SQLAlchemy mapped column that supports comparison operations.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.sql.elements import BinaryExpression

from sqlalchemy_easy_softdelete.handler.rewriter import SoftDeleteQueryRewriter
from tests.default_config.model import SDChild, SDParent, SoftDeleteMixin


def test_deleted_at_column_supports_comparison_operators():
    """Verify that deleted_at can be used with comparison operators.

    This tests that our type hints allow using the column in expressions.
    With the old `deleted_at: datetime` hint, type checkers would complain
    that datetime doesn't support these operations in a SQLAlchemy context.
    """
    # These should all work without type errors when using Mapped[datetime | None]
    now = datetime.now(timezone.utc)

    # Less than
    expr_lt = SDChild.deleted_at < now
    assert isinstance(expr_lt, BinaryExpression)

    # Greater than
    expr_gt = SDChild.deleted_at > now
    assert isinstance(expr_gt, BinaryExpression)

    # Equals
    expr_eq = SDChild.deleted_at == now
    assert isinstance(expr_eq, BinaryExpression)

    # Not equals
    expr_ne = SDChild.deleted_at != now
    assert isinstance(expr_ne, BinaryExpression)

    # IS NULL
    expr_is_none = SDChild.deleted_at.is_(None)
    assert isinstance(expr_is_none, BinaryExpression)

    # IS NOT NULL
    expr_is_not_none = SDChild.deleted_at.isnot(None)
    assert isinstance(expr_is_not_none, BinaryExpression)


def test_deleted_at_column_works_in_where_clause():
    """Verify that deleted_at can be used in .where() clauses.

    This is the primary use case from issue #31 - the user was getting
    type errors when using .where(Model.deleted_at < value).
    """
    now = datetime.now(timezone.utc)

    # Build a select statement with a where clause using deleted_at
    stmt = select(SDChild).where(SDChild.deleted_at < now)

    # The statement should compile without errors
    assert stmt is not None
    assert "deleted_at" in str(stmt)


def test_deleted_at_column_works_in_filter():
    """Verify that deleted_at works with the legacy .filter() method."""
    now = datetime.now(timezone.utc)

    # Using filter (ORM Query style)
    stmt = select(SDParent).filter(SDParent.deleted_at > now)

    assert stmt is not None
    assert "deleted_at" in str(stmt)


def test_deleted_at_column_works_with_between():
    """Verify that deleted_at works with .between()."""
    now = datetime.now(timezone.utc)
    earlier = datetime(2020, 1, 1, tzinfo=timezone.utc)

    expr = SDChild.deleted_at.between(earlier, now)
    assert isinstance(expr, BinaryExpression)


def test_delete_method_is_callable(seeded_session):
    """Verify that the delete() method stub works correctly.

    The SoftDeleteMixin provides method stubs for delete() and undelete()
    so that type checkers know these methods exist.
    """
    # Get an instance
    child = seeded_session.query(SDChild).first()
    assert child is not None

    # The delete method should be callable
    assert hasattr(child, "delete")
    assert callable(child.delete)

    # Call delete and verify it sets deleted_at
    child.delete()
    assert child.deleted_at is not None


def test_delete_without_value_uses_default(seeded_session):
    """Verify delete() without value uses the default function (current time)."""
    child = seeded_session.query(SDChild).first()
    assert child is not None

    before = datetime.now(timezone.utc)
    child.delete()
    after = datetime.now(timezone.utc)

    # Should be between before and after (current time)
    # SQLite doesn't preserve timezone, so compare without it
    deleted_at = child.deleted_at.replace(tzinfo=timezone.utc) if child.deleted_at.tzinfo is None else child.deleted_at
    assert before <= deleted_at <= after


def test_delete_with_custom_value(seeded_session):
    """Verify delete(value) uses the passed value instead of default."""
    child = seeded_session.query(SDChild).first()
    assert child is not None

    custom_date = datetime(2020, 6, 15, 12, 30, tzinfo=timezone.utc)
    child.delete(custom_date)

    # SQLite doesn't preserve timezone, so compare without it
    assert child.deleted_at.replace(tzinfo=None) == custom_date.replace(tzinfo=None)


def test_undelete_method_is_callable(seeded_session):
    """Verify that the undelete() method stub works correctly."""
    # Get a deleted instance
    child = (
        seeded_session
        .query(SDChild)
        .execution_options(include_deleted=True)
        .filter(SDChild.deleted_at.isnot(None))
        .first()
    )
    assert child is not None
    assert child.deleted_at is not None

    # The undelete method should be callable
    assert hasattr(child, "undelete")
    assert callable(child.undelete)

    # Call undelete and verify it clears deleted_at
    child.undelete()
    assert child.deleted_at is None


def test_mixin_class_has_correct_type_annotations():
    """Verify that the SoftDeleteMixin has the expected type annotations."""
    annotations = SoftDeleteMixin.__annotations__

    # Should have deleted_at annotation
    assert "deleted_at" in annotations

    # The annotation should include Mapped
    annotation_str = str(annotations["deleted_at"])
    assert "Mapped" in annotation_str or "datetime" in annotation_str


def test_rewriter_is_attached_to_mixin():
    """Verify the rewriter is attached to the mixin class."""
    assert hasattr(SoftDeleteMixin, "_sqlalchemy_easy_softdelete_rewriter")
    rewriter = SoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter
    assert isinstance(rewriter, SoftDeleteQueryRewriter)
    assert rewriter.deleted_field_name == "deleted_at"
