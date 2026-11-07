"""A pytest plugin that records every ``SoftDeleteQueryRewriter.rewrite_statement()`` call of a test run.

Each call is one JSON line: the test, the statement's SQL, the rewritten statement's SQL for PostgreSQL and for
SQLite, and whether the statement came back unchanged. Each test then gets one more line: the cache counters of
each rewriter it used, as they are at its end. A change to the rewriter that changes nothing produces the same
file, so comparing the file of two commits shows any difference in what the rewriter does, also where the test
suite's own assertions do not look.

A test's rewrites are listed sorted, not in the order they ran: SQLAlchemy runs some loads in an order that depends
on where objects sit in memory (it iterates a mapper's properties as a set), so that order changes with any change
to what the process allocates. The counters at a test's end are totals, which do not depend on it. The counters of
a test that rewrites from several threads are left out: they depend on scheduling.

Load it with ``-p rewrite_fingerprint`` and ``tools/`` on ``PYTHONPATH``; ``FINGERPRINT_OUT`` is the output file.
``tools/fingerprint.py`` runs it and compares two files.
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import threading
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from sqlalchemy_easy_softdelete.handler.rewriter import SoftDeleteQueryRewriter

_records: list[dict[str, Any]] = []
_current: dict[str, str | None] = {"test": None}
_original = SoftDeleteQueryRewriter.rewrite_statement


def _sql(stmt: Any, dialect: Any) -> str:
    try:
        return str(stmt.compile(dialect=dialect))
    except Exception as exc:  # noqa: BLE001 -- a statement that does not compile is recorded as such
        return f"<{type(exc).__name__}>"


def _recording(self: SoftDeleteQueryRewriter, stmt: Any, *args: Any, **kwargs: Any) -> Any:
    rewritten = _original(self, stmt, *args, **kwargs)
    info = self.rewrite_cache_info()
    _records.append({
        "rewrite": {
            "test": _current["test"],
            "in": _sql(stmt, postgresql.dialect()),
            "pg": _sql(rewritten, postgresql.dialect()),
            "sqlite": _sql(rewritten, sqlite.dialect()),
            "same_object": rewritten is stmt,
        },
        "cache": [info.hits, info.misses, info.uncacheable, info.size],
        "rewriter": id(self),
        "thread": threading.get_ident(),
    })
    return rewritten


SoftDeleteQueryRewriter.rewrite_statement = _recording  # type: ignore[method-assign]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Any:
    _current["test"] = item.nodeid
    yield


def pytest_sessionfinish(session: pytest.Session) -> None:
    path = os.environ.get("FINGERPRINT_OUT", "fingerprint.jsonl")
    by_test: dict[str | None, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in _records:
        by_test[record["rewrite"]["test"]].append(record)
    with open(path, "w") as out:
        for test, records in by_test.items():
            for line in sorted(json.dumps(record["rewrite"], sort_keys=True) for record in records):
                out.write(line + "\n")
            if len({record["thread"] for record in records}) == 1:
                counters_at_end = {record["rewriter"]: record["cache"] for record in records}
                summary = {"test": test, "cache_at_end": sorted(counters_at_end.values())}
                out.write(json.dumps(summary, sort_keys=True) + "\n")
    with open(path, "rb") as written:
        digest = hashlib.sha256(written.read()).hexdigest()[:16]
    print(f"\nrewrite fingerprint: {len(_records)} rewrites, sha256 {digest} -> {path}")
