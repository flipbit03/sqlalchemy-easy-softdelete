"""The soft-delete query rewriter.

``SoftDeleteQueryRewriter`` (``rewriter``) rewrites a statement in two phases: the planner (``planner``, with
``analysis``) reads it and decides what the rewrite adds, as a ``SelectPlan`` (``plan``) remembered by statement
shape; the transformations (``transform``) build the rewritten statement from it. ``policy`` holds what the
configuration decides, ``context`` where the statement being rewritten sits, and ``sa`` everything that depends on
SQLAlchemy's private API. See ARCHITECTURE.md.

This module keeps the names it has always had: instances pickled by earlier versions refer to some of them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy_easy_softdelete.handler.rewriter.context import LOAD_PATH, RELATIONSHIP_LOAD, RewriteContext
from sqlalchemy_easy_softdelete.handler.rewriter.criteria_functions import function_named
from sqlalchemy_easy_softdelete.handler.rewriter.plan import DEFAULT_REWRITE_CACHE_SIZE, RewriteCacheInfo
from sqlalchemy_easy_softdelete.handler.rewriter.rewriter import SoftDeleteQueryRewriter, Statement
from sqlalchemy_easy_softdelete.handler.rewriter.transform.criteria import (
    _unpickle_mapped_subquery_criteria,
    _unpickle_soft_delete_criteria,
)

__all__ = [
    "DEFAULT_REWRITE_CACHE_SIZE",
    "LOAD_PATH",
    "RELATIONSHIP_LOAD",
    "RewriteCacheInfo",
    "RewriteContext",
    "SoftDeleteQueryRewriter",
    "Statement",
    "_unpickle_mapped_subquery_criteria",
    "_unpickle_soft_delete_criteria",
]


def __getattr__(name: str) -> Any:
    """Find the criteria functions by name, as pickle does (see ``criteria_functions.function_named``)."""
    function = function_named(name)
    if function is not None:
        return function
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
