"""``SoftDeleteQueryRewriter``: the rewrite of a statement, by the kind of statement it is."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import FromStatement
from sqlalchemy.sql import CompoundSelect, Executable, Select
from sqlalchemy.sql.lambdas import LambdaElement
from sqlalchemy.sql.selectable import SelectBase, SelectStatementGrouping

from sqlalchemy_easy_softdelete.handler.rewriter.clauses import FILTERED_MARK
from sqlalchemy_easy_softdelete.handler.rewriter.plan import (
    DEFAULT_REWRITE_CACHE_SIZE,
    LOAD_PATH,
    RELATIONSHIP_LOAD,
    ROOT,
    PlanCache,
    RewriteCacheInfo,
    RewriteContext,
)
from sqlalchemy_easy_softdelete.handler.rewriter.planner import plan_for
from sqlalchemy_easy_softdelete.handler.rewriter.policy import SoftDeletePolicy
from sqlalchemy_easy_softdelete.handler.rewriter.transform import apply_plan
from sqlalchemy_easy_softdelete.hook import IgnoredTable

# Type alias for statements we can rewrite
if TYPE_CHECKING:
    from typing_extensions import TypeAlias, Unpack

    # SQLAlchemy 2.1's statements take a type parameter for each column: these have any columns.
    Statement: TypeAlias = (
        Select[Unpack[tuple[Any, ...]]]
        | FromStatement[Unpack[tuple[Any, ...]]]
        | CompoundSelect[Unpack[tuple[Any, ...]]]
        | Executable
        | LambdaElement
    )
else:
    Statement = Select | FromStatement | CompoundSelect | Executable | LambdaElement


class SoftDeleteQueryRewriter:
    """Rewrites SQL statements based on configuration.

    Rewriting never modifies the statement it is given, nor any object inside it: it returns a new
    statement. Applications can therefore reuse statements, subqueries and ``lambda_stmt`` objects
    across executions, with and without soft-delete filtering. The new statement differs only in its
    FROM clause, where filtered copies of subqueries replace them, and keeps the caller's columns,
    entities and options, which SQLAlchemy keys result rows by (``row._mapping[subquery.c.x]``).

    Finding the tables of an ORM SELECT means running SQLAlchemy's ORM compilation of it, outside of
    SQLAlchemy's compiled cache. The rewriter remembers what it adds to a statement by the statement's
    cache key, which SQLAlchemy gives two statements only when they compile to the same SQL, so later
    statements of the same shape skip that step.

    Each public method takes an optional ``context`` (see ``RewriteContext``): the hook passes the one of the
    execution; without it, the statement is rewritten as one of its own.
    """

    def __init__(
        self,
        deleted_field_name: str,
        disable_soft_delete_option_name: str,
        ignored_tables: list[IgnoredTable] | None = None,
        rewrite_cache_size: int = DEFAULT_REWRITE_CACHE_SIZE,
    ):
        """
        Instantiate a new query rewriter.

        Params:

        deleted_field_name:
            The name of the field that should be present in a table for soft-deletion
            rewriting to occur

        disable_soft_delete_option_name:
            Execution option name (to use with .execution_options(xxxx=True) to disable
            soft deletion rewriting in a query

        ignored_tables:
            Tables that are never filtered, even when they have the soft-delete field

        rewrite_cache_size:
            How many statement shapes to remember the rewrite of; 0 disables the cache

        """
        if rewrite_cache_size < 0:
            raise ValueError(f"rewrite_cache_size must be 0 or more, got {rewrite_cache_size}")

        self.policy = SoftDeletePolicy(deleted_field_name, disable_soft_delete_option_name, ignored_tables or [])
        self._lambda_cache_token = _LambdaRewriteToken(deleted_field_name)
        self._plan_cache = PlanCache(rewrite_cache_size) if rewrite_cache_size else None

    @property
    def deleted_field_name(self) -> str:
        """The name of the soft-delete field."""
        return self.policy.deleted_field_name

    @deleted_field_name.setter
    def deleted_field_name(self, deleted_field_name: str) -> None:
        self.policy.deleted_field_name = deleted_field_name

    @property
    def disable_soft_delete_option_name(self) -> str:
        """The execution option that disables soft-delete filtering."""
        return self.policy.disable_soft_delete_option_name

    @disable_soft_delete_option_name.setter
    def disable_soft_delete_option_name(self, disable_soft_delete_option_name: str) -> None:
        self.policy.disable_soft_delete_option_name = disable_soft_delete_option_name

    @property
    def ignored_tables(self) -> list[IgnoredTable]:
        """Tables that are never filtered."""
        return self.policy.ignored_tables

    @ignored_tables.setter
    def ignored_tables(self, ignored_tables: list[IgnoredTable]) -> None:
        self.policy.ignored_tables = ignored_tables

    def rewrite_cache_info(self) -> RewriteCacheInfo:
        """Hits, misses and size of the rewrite cache, to help choose ``rewrite_cache_size``."""
        if self._plan_cache is None:
            return RewriteCacheInfo(hits=0, misses=0, uncacheable=0, size=0, max_size=0)
        return self._plan_cache.info()

    def rewrite_statement(self, stmt: Statement, context: RewriteContext | None = None) -> Statement:
        """Rewrite a single SQL-like Statement."""
        if context is None:
            # As RewriteContext.ambient(), inlined: every rewrite comes in here.
            relationship_load, load_path = RELATIONSHIP_LOAD.get(), LOAD_PATH.get()
            context = (
                ROOT if not relationship_load and load_path is None else RewriteContext(relationship_load, load_path)
            )
        if isinstance(stmt, Select):
            return self.rewrite_select(stmt, context)

        # Handle CompoundSelect
        if isinstance(stmt, CompoundSelect):
            return self.rewrite_compound_select(stmt, context)

        # Handle LambdaElement (StatementLambdaElement, LinkedLambdaElement)
        # Used by lambda_stmt() for statement caching (e.g., advanced-alchemy)
        if isinstance(stmt, LambdaElement):
            return self.rewrite_lambda_element(stmt, context)

        # Handle FromStatement which is also a Select/Executable
        if isinstance(stmt, FromStatement):
            return self._rewrite_from_statement(stmt, context)

        raise NotImplementedError(f'Unsupported statement type "{(type(stmt))}"!')

    def _rewrite_from_statement(self, stmt: FromStatement[Any], context: RewriteContext) -> Statement:
        if self.policy.is_disabled(stmt):
            return stmt
        # The element is what SQLAlchemy compiles: loader criteria apply from there.
        element: Any
        if isinstance(stmt.element, Select):
            element = self.rewrite_select(stmt.element, context)
        elif isinstance(stmt.element, CompoundSelect):
            element = self.rewrite_compound_select(stmt.element, context)
        else:
            # INSERT/UPDATE/DELETE ... RETURNING, and textual SQL, are left as they are.
            return stmt
        if element is stmt.element:
            return stmt
        rewritten: FromStatement[Any] = stmt._generate()
        rewritten.element = element
        return rewritten

    def rewrite_lambda_element(self, stmt: LambdaElement, context: RewriteContext | None = None) -> LambdaElement:
        """Rewrite a LambdaElement (from lambda_stmt()).

        LambdaElement wraps a SQL statement for caching purposes. The actual
        statement is the one it resolves to. The rewritten statement
        is set on a copy of the LambdaElement; the one passed in is left as it was.

        Supports both StatementLambdaElement (basic lambda_stmt) and
        LinkedLambdaElement (chained lambda operations).
        """
        if context is None:
            context = RewriteContext.ambient()
        # Get the resolved statement - this is the actual Select/CompoundSelect
        resolved = getattr(stmt, "_resolved", stmt)

        # Rewrite based on the type of the resolved statement
        rewritten_resolved: Any
        if isinstance(resolved, Select):
            rewritten_resolved = self.rewrite_select(resolved, context)
        elif isinstance(resolved, CompoundSelect):
            rewritten_resolved = self.rewrite_compound_select(resolved, context)
        elif isinstance(resolved, FromStatement):
            rewritten_resolved = self.rewrite_statement(resolved, context)
        else:
            raise NotImplementedError(f'Unsupported statement type "{type(resolved)}" inside LambdaElement._resolved')

        if rewritten_resolved is resolved:
            return stmt
        rewritten = stmt._clone()
        rewritten._resolved = rewritten_resolved
        # SQLAlchemy caches the compiled form of a lambda statement by the lambda's code and closure,
        # not by its resulting statement, so the copy would share the compiled form of the statement
        # without soft-delete filtering (from a Core execution of the same lambda, which this hook does
        # not rewrite). This rewriter's token in the cache key keeps the two apart. The rewrite only
        # adds IS NULL criteria, so the lambda's bound parameters stay the statement's parameters.
        if isinstance(rewritten.closure_cache_key, tuple):
            rewritten.closure_cache_key = rewritten.closure_cache_key + (self._lambda_cache_token,)
        return rewritten

    def rewrite_select(
        self, stmt: Select[Unpack[tuple[Any, ...]]], context: RewriteContext | None = None
    ) -> Select[Unpack[tuple[Any, ...]]]:
        """Rewrite a Select Statement."""
        # if the user tagged this query with an execution_option to disable soft-delete filtering
        # simply return back the same stmt
        if self.policy.is_disabled(stmt):
            return stmt

        if context is None:
            context = RewriteContext.ambient()
        plan, analysis, key = plan_for(self, stmt, context, self._plan_cache)
        rewritten = apply_plan(self, stmt, plan, analysis, key, context)
        if rewritten is not stmt and context.enclosing_froms:
            # A nested SELECT, filtered here: the loader criteria leave it out, so that it renders as the same
            # expression filtered by them does (in GROUP BY), not filtered twice.
            setattr(rewritten, FILTERED_MARK, True)
        return rewritten  # type: ignore[no-any-return]

    def rewrite_compound_select(
        self, stmt: CompoundSelect[Unpack[tuple[Any, ...]]], context: RewriteContext | None = None
    ) -> CompoundSelect[Unpack[tuple[Any, ...]]]:
        """Rewrite a Compound Select Statement."""
        if self.policy.is_disabled(stmt):
            return stmt
        if context is None:
            context = RewriteContext.ambient()
        selects: list[SelectBase] = []
        nested_key = context.nested_key if context.enclosing_froms else None
        for place, select in enumerate(stmt.selects):
            # Nested in a statement, the plan of each member is remembered by its own place in the union.
            member_context = context.keyed((nested_key, "member", place) if nested_key is not None else None)
            selects.append(self._rewrite_compound_member(select, member_context))
        if all(new is old for new, old in zip(selects, stmt.selects)):
            return stmt
        rewritten = stmt._generate()
        rewritten.selects = selects
        return rewritten

    def _rewrite_compound_member(self, member: Any, context: RewriteContext) -> Any:
        """Rewrite one SELECT of a union: a SELECT or a nested union, possibly in parentheses (a member
        with ORDER BY or LIMIT, or a nested union, is held in a grouping)."""
        if isinstance(member, SelectStatementGrouping):
            element = self._rewrite_compound_member(member.element, context)
            return member if element is member.element else SelectStatementGrouping(element)
        if isinstance(member, CompoundSelect):
            return self.rewrite_compound_select(member, context)
        if not isinstance(member, Select):
            # A member of textual SQL (text(...).columns(...)): there is nothing to filter.
            return member
        return self.rewrite_select(member, context)


class _LambdaRewriteToken:
    """Marks, in a ``lambda_stmt`` cache key, the statements one rewriter has rewritten."""

    __slots__ = ("field_name",)

    def __init__(self, field_name: str) -> None:
        self.field_name = field_name

    def __repr__(self) -> str:
        return f"<soft-delete rewrite of {self.field_name!r} at {id(self):#x}>"
