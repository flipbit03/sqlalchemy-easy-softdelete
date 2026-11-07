"""The soft-delete query rewriter. See ARCHITECTURE.md for how its modules fit together.

Pickled instances refer to ``_unpickle_mapped_subquery_criteria``, ``_unpickle_soft_delete_criteria`` and the
criteria functions (``_is_not_soft_deleted_<hex>``) by this module's path, so they are found here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy_easy_softdelete.handler.rewriter.plan import (
    DEFAULT_REWRITE_CACHE_SIZE,
    LOAD_PATH,
    RELATIONSHIP_LOAD,
    RewriteCacheInfo,
    RewriteContext,
)
from sqlalchemy_easy_softdelete.handler.rewriter.policy import function_named
from sqlalchemy_easy_softdelete.handler.rewriter.rewriter import SoftDeleteQueryRewriter, Statement
from sqlalchemy_easy_softdelete.handler.rewriter.transform import (
    _unpickle_mapped_subquery_criteria,
    _unpickle_soft_delete_criteria,
)
from sqlalchemy_easy_softdelete.hook import IgnoredTable

__all__ = [
    "DEFAULT_REWRITE_CACHE_SIZE",
    "LOAD_PATH",
    "RELATIONSHIP_LOAD",
    "IgnoredTable",
    "RewriteCacheInfo",
    "RewriteContext",
    "SoftDeleteQueryRewriter",
    "Statement",
    "_unpickle_mapped_subquery_criteria",
    "_unpickle_soft_delete_criteria",
]


def __getattr__(name: str) -> Any:
    """The criteria functions, by the name pickle stores them under."""
    function = function_named(name)
    if function is not None:
        return function
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
