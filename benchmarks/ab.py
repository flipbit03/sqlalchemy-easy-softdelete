"""Compare the rewriter's speed between two checkouts, alternating runs: A, B, A, B, ...

    python benchmarks/ab.py BASE_DIR CANDIDATE_DIR [--python PYTHON] [--rounds 5]

Each round runs ``bench_rewrite.py`` against each checkout's package (``BASE_DIR`` and ``CANDIDATE_DIR`` hold a
``sqlalchemy_easy_softdelete`` package). For each shape and figure, the table shows the median over the rounds and
the candidate/base ratio. The gates: ``warm`` and ``cold`` rewrites at most 1.05x, ``execute`` at most 1.03x; the
exit status is 1 when one fails. Make a base checkout with ``git worktree add /tmp/base <commit>``.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent / "bench_rewrite.py"
GATES = {"warm": 1.05, "cold": 1.05, "execute": 1.03}
MEMORY_LIMIT = 3 * 1024**3


def _limit_memory() -> None:
    # A shape gone wrong (a query that never ends) fails on its own instead of exhausting the machine's memory.
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))


def run(code: Path, python: str) -> dict[str, dict[str, float]]:
    env = dict(os.environ, PYTHONPATH=str(code.resolve()))
    output = subprocess.run(
        [python, str(BENCH)], env=env, check=True, capture_output=True, text=True, preexec_fn=_limit_memory, timeout=900
    ).stdout
    return json.loads(output.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()

    samples: dict[str, list[dict[str, dict[str, float]]]] = {"base": [], "candidate": []}
    for round_ in range(args.rounds):
        for label, code in (("base", args.base), ("candidate", args.candidate)):
            samples[label].append(run(code, args.python))
        print(f"round {round_ + 1}/{args.rounds} done", file=sys.stderr)

    failed = False
    print("| Shape | Figure | Base µs | Candidate µs | Ratio | Gate |\n|---|---|---:|---:|---:|---|")
    for shape, figures in samples["base"][0].items():
        for figure in figures:
            base = statistics.median(run_[shape][figure] for run_ in samples["base"])
            candidate = statistics.median(run_[shape][figure] for run_ in samples["candidate"])
            ratio = candidate / base
            ok = ratio <= GATES[figure]
            failed = failed or not ok
            print(f"| {shape} | {figure} | {base:.1f} | {candidate:.1f} | {ratio:.3f} | {'ok' if ok else 'FAIL'} |")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
