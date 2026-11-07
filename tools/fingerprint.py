"""Record and compare rewrite fingerprints (see ``rewrite_fingerprint.py``).

    python tools/fingerprint.py record OUT [--python PYTHON] [--db URL] [--code DIR]
    python tools/fingerprint.py compare BASE CANDIDATE

``record`` runs the test suite of ``--code`` (default: this checkout) with the plugin. ``--python`` picks the
interpreter, and so the SQLAlchemy version; ``--db`` is the ``TEST_CONNECTION_STRING`` (default: SQLite).
``compare`` exits with status 1, and shows the first differences, when the two files differ.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent


def record(out: Path, python: str, db: str | None, code: Path) -> int:
    env = dict(os.environ, FINGERPRINT_OUT=str(out.resolve()), PYTHONPATH=str(TOOLS))
    env.pop("TEST_CONNECTION_STRING", None)
    if db:
        env["TEST_CONNECTION_STRING"] = db
    command = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "rewrite_fingerprint", "tests"]
    return subprocess.call(command, cwd=code, env=env)


def compare(base: Path, candidate: Path) -> int:
    base_lines = base.read_text().splitlines()
    candidate_lines = candidate.read_text().splitlines()
    differing = [(index, old, new) for index, (old, new) in enumerate(zip(base_lines, candidate_lines)) if old != new]
    if not differing and len(base_lines) == len(candidate_lines):
        print(f"identical: {len(base_lines)} records")
        return 0
    print(f"DIFFERENT: {len(base_lines)} vs {len(candidate_lines)} records, {len(differing)} differ")
    for index, old, new in differing[:5]:
        old_record, new_record = json.loads(old), json.loads(new)
        fields = [key for key in old_record if old_record.get(key) != new_record.get(key)]
        print(f"- record {index} ({old_record.get('test')}): {', '.join(fields)}")
        for field in fields:
            print(f"    base:      {str(old_record.get(field))[:300]}")
            print(f"    candidate: {str(new_record.get(field))[:300]}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    rec = commands.add_parser("record")
    rec.add_argument("out", type=Path)
    rec.add_argument("--python", default=sys.executable)
    rec.add_argument("--db")
    rec.add_argument("--code", type=Path, default=TOOLS.parent)
    cmp_ = commands.add_parser("compare")
    cmp_.add_argument("base", type=Path)
    cmp_.add_argument("candidate", type=Path)
    args = parser.parse_args()
    if args.command == "record":
        return record(args.out, args.python, args.db, args.code)
    return compare(args.base, args.candidate)


if __name__ == "__main__":
    sys.exit(main())
