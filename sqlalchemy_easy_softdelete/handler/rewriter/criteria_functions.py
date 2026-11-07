"""The loader criteria function of soft-deletable classes: ``lambda cls: cls.<field_name>.is_(None)``."""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Any

PICKLE_MODULE = "sqlalchemy_easy_softdelete.handler.rewriter"
"""The module pickle finds the criteria functions in (see ``function_named``): the rewriter's module before it
became a package, which instances pickled by earlier versions refer to."""

_CRITERIA_TEMPLATE = (lambda cls: cls._soft_delete_field_.is_(None)).__code__
_CRITERIA_PREFIX = "_is_not_soft_deleted_"
_criteria_functions: dict[str, Callable[[Any], Any]] = {}


def is_null_criteria(field_name: str) -> Callable[[Any], Any]:
    """``lambda cls: cls.<field_name>.is_(None)``, for ``with_loader_criteria``.

    SQLAlchemy calls the lambda with each entity and each ``aliased()`` form of it, so the filter is
    built against the alias itself. A plain expression would have to be adapted to the alias, which
    SQLAlchemy does not do for an explicit ``join(alias, onclause)`` and renders invalid SQL.

    The field name cannot be a closure variable: SQLAlchemy replaces those with trackers while it
    analyzes the lambda, and caches the analyzed lambda by its code object, so a default argument
    would leak one configuration's field name into another's. The name is written into the code
    object instead, which gives each field name its own code, and so its own cache entries. Each field
    name has a single function, named after it, which pickle can find again (see ``function_named``).
    """
    name = _CRITERIA_PREFIX + field_name.encode().hex()
    function = _criteria_functions.get(name)
    if function is None:
        code = _CRITERIA_TEMPLATE.replace(
            co_names=tuple(field_name if n == "_soft_delete_field_" else n for n in _CRITERIA_TEMPLATE.co_names)
        )
        candidate = types.FunctionType(code, {}, name)
        candidate.__qualname__ = name
        candidate.__module__ = PICKLE_MODULE
        function = _criteria_functions.setdefault(name, candidate)
    return function


def function_named(name: str) -> Callable[[Any], Any] | None:
    """The criteria function pickle stored as ``name`` (``_is_not_soft_deleted_<hex of the field name>``), or None.

    Instances keep the loader criteria of the query that loaded them, for their lazy loads, so pickling
    an instance pickles them, and pickle stores a function as its module and name.
    """
    if name.startswith(_CRITERIA_PREFIX):
        try:
            field_name = bytes.fromhex(name[len(_CRITERIA_PREFIX) :]).decode()
        except ValueError:
            return None
        return is_null_criteria(field_name)
    return None
