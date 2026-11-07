# Architecture

A `do_orm_execute` hook (`handler/sqlalchemy_easy_softdelete.py`) hands each ORM SELECT to
`SoftDeleteQueryRewriter`, which returns a filtered copy; the original statement is never modified.

The rewriter (`handler/rewriter/`) rewrites a SELECT in two steps. The planner reads the statement and makes a
`SelectPlan`, remembered by the statement's cache key. The transformations then build the rewritten statement
from the plan. Subqueries, CTEs, nested SELECTs and union members are rewritten the same way, recursively, each
with a `RewriteContext` that says where it sits.

| Module | Holds |
|---|---|
| `rewriter.py` | `SoftDeleteQueryRewriter`: dispatch by statement kind (SELECT, union, `from_statement()`, `lambda_stmt()`). |
| `planner.py` | `plan_for()`: a cached plan for the statement's shape, or a new one from the analysis. |
| `analysis.py` | Reading a statement: FROM clause, eager loading and the mapping, secondaries, outer joins, nested SELECTs. |
| `transform.py` | Building the rewritten statement: filtered copies, nested SELECTs, loader criteria, ON and WHERE filters. |
| `policy.py` | What the configuration decides: soft-deletable tables and classes, their conditions, disabled statements. |
| `plan.py` | `SelectPlan`, the `Reason`s a plan is not cached, the plan cache, and `RewriteContext`. |
| `clauses.py` | Helpers over SQLAlchemy clauses, and the workarounds for differences between SQLAlchemy releases. |

Imports go one way: `rewriter` → `planner` → `transform` → `analysis` → `policy`, `plan` → `clauses`. The analysis
never builds a statement.

A plan is cached only when it holds for every statement with the same cache key. Each case where it doesn't
(it depends on execution options, or on the statement's own aliases and subqueries) is a `Reason` in `plan.py`;
such a plan is counted as `uncacheable` in `rewrite_cache_info()`.

Pickled instances refer to `_unpickle_*` and `_is_not_soft_deleted_<hex>` at
`sqlalchemy_easy_softdelete.handler.rewriter`, which re-exports them; that path must not change.
