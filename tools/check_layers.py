"""Check the rewriter package's architecture (see ARCHITECTURE.md): exit status 1, with the violations, otherwise 0.

    python tools/check_layers.py

1. Layers. A module imports only from its own layer or the layers below it, never above:
   sa < (criteria_functions, context, plan, policy) < analysis < transform < planner < rewriter < the package.
   Imports under ``if TYPE_CHECKING:`` are for type checkers only, and do not count.
2. No import cycles between the package's modules.
3. SQLAlchemy's private API. Only the modules of ``sa/`` read or write ``_``-prefixed attributes of other objects
   (``self`` and ``cls`` are a module's own; ``super()`` is the class it extends).

It reads the source with ``ast``, without importing it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE = "sqlalchemy_easy_softdelete.handler.rewriter"
ROOT = Path(__file__).resolve().parent.parent / "sqlalchemy_easy_softdelete" / "handler" / "rewriter"

LAYERS = {
    "sa": 0,
    "criteria_functions": 1,
    "context": 1,
    "plan": 1,
    "policy": 1,
    "analysis": 2,
    "transform": 3,
    "planner": 4,
    "rewriter": 5,
    "": 6,  # the package's own __init__ (the façade)
}

ALLOWED_PRIVATE = {"_soft_delete_field_"}
"""Private-looking names that are not SQLAlchemy's: the placeholder of the criteria function template."""


def module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def layer_of(name: str) -> int:
    return LAYERS[name.split(".")[0]]


def type_checking_nodes(tree: ast.AST) -> set[int]:
    """The ids of the nodes under ``if TYPE_CHECKING:``."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            for child in node.body:
                found.update(id(each) for each in ast.walk(child))
    return found


def internal_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """The package modules a module imports at runtime, with the line of each import."""
    skipped = type_checking_nodes(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(PACKAGE):
            target = node.module[len(PACKAGE) :].lstrip(".")
            found.append((node.lineno, target))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PACKAGE + "."):
                    found.append((node.lineno, alias.name[len(PACKAGE) + 1 :]))
    return found


def private_accesses(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        name = node.attr
        if not name.startswith("_") or name.startswith("__") or name in ALLOWED_PRIVATE:
            continue
        owner = node.value
        if isinstance(owner, ast.Name) and owner.id in ("self", "cls"):
            continue
        if isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name) and owner.func.id == "super":
            continue
        found.append((node.lineno, name))
    for node in ast.walk(tree):
        # getattr(element, "_private", ...) is an access too.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "setattr", "hasattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value.startswith("_")
            and not node.args[1].value.startswith("__")
        ):
            found.append((node.lineno, node.args[1].value))
    return found


def main() -> int:
    problems: list[str] = []
    graph: dict[str, set[str]] = {}
    for path in sorted(ROOT.rglob("*.py")):
        name = module_name(path)
        tree = ast.parse(path.read_text(), str(path))
        shown = path.relative_to(ROOT.parent.parent.parent)
        graph[name] = set()
        for line, target in internal_imports(tree):
            module = target if target else ""
            graph[name].add(module)
            if layer_of(module) > layer_of(name):
                problems.append(f"{shown}:{line}: imports {module or PACKAGE}, from a layer above")
        if not name.startswith("sa"):
            for line, attr in private_accesses(tree):
                problems.append(f"{shown}:{line}: uses {attr}, private SQLAlchemy API outside sa/")
    problems.extend(_cycles(graph))
    for problem in problems:
        print(problem)
    if not problems:
        print(f"layers ok: {len(graph)} modules")
    return 1 if problems else 0


def _cycles(graph: dict[str, set[str]]) -> list[str]:
    """Import cycles between modules (a package's ``__init__`` counts as the package)."""
    found: list[str] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(module: str) -> None:
        state[module] = 1
        stack.append(module)
        for target in sorted(graph.get(module, ())):
            target = target if target in graph else target.rsplit(".", 1)[0]
            if state.get(target) == 1:
                found.append("import cycle: " + " -> ".join(stack[stack.index(target) :] + [target]))
            elif state.get(target) is None and target in graph:
                visit(target)
        stack.pop()
        state[module] = 2

    for module in sorted(graph):
        if state.get(module) is None:
            visit(module)
    return found


if __name__ == "__main__":
    sys.exit(main())
