"""Static guard for the git invocation contract (issue #67).

``src/git_run.py::run_git`` is the one place that spawns ``git`` with
``GIT_OPTIONAL_LOCKS=0``, so a read killed mid-``git status`` can't strand a
0-byte ``index.lock`` (fleet-config#667, #939). A new direct spawn elsewhere
bypasses that silently, so this fails until it is routed through ``run_git``
or added to ``ALLOWED`` below as a deliberate decision.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANNED_DIRS = ("src", "app", "scripts")

#: Repo-relative paths allowed to spawn git themselves. ``git_run`` is the
#: contract; ``classify_e2e`` runs outside the app's import path and sets the
#: same env inline (pinned in tests/test_classify_e2e.py).
ALLOWED = {
    "src/git_run.py", "scripts/classify_e2e.py", "src/build_info.py",
    "scripts/docs_ratchet.py",
}

#: Allowlisted files that are the scaffold's verbatim copy and so cannot route
#: through ``run_git`` (a byte-identical copy must stay self-contained). Their
#: one spawn is a read-only ``rev-parse`` (build_info) or ``ls-files``
#: (docs_ratchet, which also sets ``GIT_OPTIONAL_LOCKS=0`` inline). Each must be
#: declared in ``.fleet.toml`` ``[vendored]``.
VENDORED_VERBATIM = {"src/build_info.py", "scripts/docs_ratchet.py"}


def _is_git_argv(node: ast.AST) -> bool:
    """``["git", …]`` / ``("git", …)`` literal, or a shell string ``"git …"``."""
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        first = node.elts[0]
        return isinstance(first, ast.Constant) and first.value == "git"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == "git" or node.value.startswith("git ")
    return False


def _direct_git_spawns(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"run", "Popen", "check_output", "check_call", "call"}:
            continue
        if node.args and _is_git_argv(node.args[0]):
            lines.append(node.lineno)
    return lines


def test_no_module_spawns_git_outside_the_allowlist():
    offenders = []
    for top in SCANNED_DIRS:
        for path in sorted((REPO_ROOT / top).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWED:
                continue
            offenders += [f"{rel}:{line}" for line in _direct_git_spawns(path)]
    assert not offenders, (
        "spawn git through src.git_run.run_git (sets GIT_OPTIONAL_LOCKS=0), "
        f"or add the file to ALLOWED with a reason: {offenders}"
    )


def test_the_scan_sees_the_allowlisted_spawns():
    """Guards the guard: if the AST matcher stopped recognising a git spawn,
    the test above would pass vacuously."""
    for rel in ALLOWED:
        assert _direct_git_spawns(REPO_ROOT / rel), f"{rel} no longer matches"


def test_the_vendored_exemption_is_declared():
    """An exempted file that is no longer a declared scaffold copy has been
    forked again, and must go back through ``run_git``."""
    manifest = (REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8")
    assert all(f'dest = "{r}"' in manifest for r in VENDORED_VERBATIM), "exemption not in [vendored]"
