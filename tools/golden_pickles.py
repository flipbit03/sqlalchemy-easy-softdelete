"""Instances pickled by an earlier version of the library still unpickle, and their lazy loads stay filtered.

    python tools/golden_pickles.py generate [DIR]   # with the version whose pickles to keep
    python tools/golden_pickles.py check [DIR]      # with the version to check

An instance keeps the loader criteria of the statement that loaded it, for its lazy loads, so pickling it pickles
the library's criteria: by the module and name of the functions that rebuild them, and of the criteria functions.
Those names must keep working. ``DIR`` defaults to ``tools/golden_pickles/sqlalchemy-<major.minor>``; the files there were made by
the version before the rewriter was restructured into a package (13680f9).
"""

from __future__ import annotations

import argparse
import pickle
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlalchemy
from golden_pickle_models import GPParent, populate
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, joinedload, selectinload

# SQLAlchemy's own objects in a pickle are those of one SQLAlchemy release line: a directory for each.
DEFAULT_DIR = (
    Path(__file__).resolve().parent
    / "golden_pickles"
    / ("sqlalchemy-" + ".".join(sqlalchemy.__version__.split(".")[:2]))
)

# Each case: how its instance is loaded, and what it loads lazily after unpickling.
CASES: dict[str, Callable[[Session], Any]] = {
    # Criteria on a soft-deletable class: a criteria function, found by name (``_is_not_soft_deleted_<hex>``).
    "selectin_loaded_parent": lambda session: session.scalars(
        select(GPParent).options(selectinload(GPParent.children))
    ).one(),
    # Criteria on a class mapped to a SELECT: rebuilt from the rewriter's configuration.
    "joined_loaded_parent": lambda session: (
        session.scalars(select(GPParent).options(joinedload(GPParent.child_names))).unique().one()
    ),
}


def _engine() -> Any:
    engine = create_engine("sqlite://")
    populate(engine)
    return engine


def generate(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    engine = _engine()
    for name, load in CASES.items():
        with Session(engine) as session:
            instance = load(session)
            session.expunge_all()
        (directory / f"{name}.pickle").write_bytes(pickle.dumps(instance, protocol=4))
        print(f"wrote {name}.pickle")


def check(directory: Path) -> int:
    engine = _engine()
    failures = 0
    for name in CASES:
        instance = pickle.loads((directory / f"{name}.pickle").read_bytes())
        with Session(engine) as session:
            session.add(instance)
            # Lazy loads after unpickling run with the unpickled criteria.
            loaded = {
                "children": [child.id for child in instance.children],
                "child_names": [child.id for child in instance.child_names],
                "lazy_children": [child.id for child in instance.lazy_children],
            }
        expected = {"children": [1], "child_names": [1], "lazy_children": [1]}
        status = "ok" if loaded == expected else "FAILED"
        failures += status != "ok"
        print(f"{status} {name}: {loaded}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["generate", "check"])
    parser.add_argument("directory", type=Path, nargs="?", default=DEFAULT_DIR)
    args = parser.parse_args()
    if args.command == "generate":
        generate(args.directory)
        return 0
    return check(args.directory)


if __name__ == "__main__":
    sys.exit(main())
