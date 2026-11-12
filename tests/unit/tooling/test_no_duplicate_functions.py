"""A function body copied into a second module is how a fix lands in one copy only.

Walks every ``src/`` and ``app/`` module (``scripts/`` holds byte-vendored scaffold copies) and fails when two
files define a module-level function whose name and whole body (docstring aside)
are identical. Share the function from the module that owns the concept instead
of pasting it next door. Nothing is baselined: a hit is a real duplicate.
"""

from __future__ import annotations

import ast
import pathlib
from collections import defaultdict
from typing import Dict, List, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
SCAN_ROOTS = ("src", "app")
# Directories whose copies are byte-vendored from the scaffold on purpose.
SKIP_PARTS = {"_vendored", "vendor", "__pycache__", ".venv", "node_modules"}
# One-liners and tiny guards are coincidental matches, not copied logic.
MIN_BODY_STATEMENTS = 3


def _body_without_docstring(node: ast.FunctionDef) -> List[ast.stmt]:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _statement_count(body: List[ast.stmt]) -> int:
    return sum(
        isinstance(n, ast.stmt) for stmt in body for n in ast.walk(stmt)
    )


def _signature(node: ast.FunctionDef) -> str:
    body = _body_without_docstring(node)
    return "|".join(
        [ast.dump(node.args), ast.dump(node.returns) if node.returns else ""]
        + [ast.dump(stmt) for stmt in body]
    )


def find_duplicates(repo_root: pathlib.Path) -> Dict[Tuple[str, str], List[str]]:
    """``(function name, body fingerprint)`` -> the files that define it."""
    seen: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for root in SCAN_ROOTS:
        for path in sorted((repo_root / root).rglob("*.py")):
            if SKIP_PARTS.intersection(path.parts):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = _body_without_docstring(node)
                if _statement_count(body) < MIN_BODY_STATEMENTS:
                    continue
                rel = path.relative_to(repo_root).as_posix()
                seen[(node.name, _signature(node))].append(rel)
    return {key: files for key, files in seen.items() if len(set(files)) > 1}


def test_no_function_body_is_copied_across_modules() -> None:
    dupes = find_duplicates(REPO_ROOT)
    report = "\n".join(
        f"  {name}() in {', '.join(sorted(set(files)))}"
        for (name, _), files in sorted(dupes.items())
    )
    assert not dupes, (
        "identical function bodies in more than one module — keep one and import "
        "it where it is needed:\n" + report
    )


def test_the_check_sees_a_copied_function(tmp_path: pathlib.Path) -> None:
    """Prove the scan fails on a real duplicate, so a green run means something."""
    lines = [
        "def parse(value):",
        "    if not value:",
        "        return None",
        "    try:",
        "        return int(value)",
        "    except ValueError:",
        "        return None",
    ]
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    documented = lines[:1] + ['    """Docs differ."""'] + lines[1:]
    (tmp_path / "src" / "b.py").write_text("\n".join(documented) + "\n", encoding="utf-8")
    assert list(find_duplicates(tmp_path).values()) == [["src/a.py", "src/b.py"]]
