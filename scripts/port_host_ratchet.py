"""Ratchet on port and host literals in scripts and code: their count may only go down.

Updating a fleet app is a clean `git pull --ff-only` plus its restart recipe,
so no machine may need to edit a tracked file to run it. Ports, hosts and
other machine-local values live in an ignored config file with a tracked
sample, read through the repo's config resolver. This stage reads the
tracked scripts and code, finds port and host literals written outside the
resolver and the samples, counts them per file against a committed baseline,
and fails only when a count rises.

The rules (docs/port-host-ratchet.md):

  * `port-literal` -- a port number written next to a host
    (`127.0.0.1:8445`, `localhost:8501`, `{host}:8445`, `%HOST%:8445`) or
    given to a port name (`PORT=8501`, `--port 8130`, `DEFAULT_PORT = 8446`,
    `-Ports "8465"`, `"port": 8000`);
  * `host-literal` -- a private-LAN or CGNAT/tailnet IPv4 address
    (`10.x`, `172.16-31.x`, `192.168.x`, `100.64-127.x`) or a `*.ts.net`
    name; a CIDR network (`100.64.0.0/10`) is not a host;
  * `bare-exempt` -- a `port-host-exempt` marker with no reason.

Scripts and code are files ending `.py`, `.ps1`, `.psm1`, `.bat`, `.cmd`,
`.sh`, `.js`, `.mjs`, `.cjs` or `.ts`. Never read: the files the repo lists in
`resolvers` (the config resolver owns the defaults), sample files (a name
holding `.sample` or `.example`), vendored dests, `exclude` globs, and test
files unless `include_tests = true` (a test booting a disposable instance on a
fixed port is not a machine-local value). Full-line comments, PowerShell
`<# ... #>` and JS `/* ... */` blocks and Python docstrings are prose and are
skipped; the hit is the value code reads.

A single hit keeps its exception with `port-host-exempt: <reason>` on its line
or on the comment line right above it. Exemptions are printed every run.

Opt-in, per adopter, in its own `.fleet.toml`:

    [port_host_ratchet]
    enabled       = true
    baseline      = ".ratchets/port-host.json"  # default
    rules         = [...]                              # default: all
    resolvers     = ["src/app_config.py"]              # globs; own the defaults
    include_tests = false                              # default
    exclude       = ["scripts/bench/**"]               # globs; never scanned

Exit codes: 0 pass or not enabled; 1 a count rose, or the baseline is missing /
a mode was refused; 2 bad configuration, baseline or arguments; 3 NOT CONFIRMED
(`git` unusable, a file that cannot be read, decoded or parsed) -- the gate
fails on it. Modes: `check`, `--init`, `--update-baseline` (shrink only),
`--update-baseline --allow-additions` (shows in review).

Vendor-verbatim (manifest key `port_host_ratchet`), stdlib-only.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import ModuleType


def _load_sibling(name: str) -> ModuleType:
    """A sibling module, loaded by path so `python -P` (scripts/ off `sys.path`) finds it."""
    try:
        if __package__:  # imported as a package member (the tests)
            return importlib.import_module(f"{__package__}.{name}")
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
        assert spec is not None and spec.loader is not None
        module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, SyntaxError):
        print(f"port host ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor both "
              "(manifest keys port_host_ratchet and ratchet_baseline)")
        sys.exit(3)


_baseline = _load_sibling("ratchet_baseline")  # where the baseline lives, and its move into .ratchets/

REPO_ROOT = Path(__file__).resolve().parent.parent

RULES: tuple[str, ...] = ("port-literal", "host-literal", "bare-exempt")
BASELINE_VERSION = 1
DEFAULT_BASELINE = ".ratchets/port-host.json"
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**")
GIT_TIMEOUT_S = 120
EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "rules", "resolvers", "include_tests", "exclude"}
CODE_SUFFIXES = (".py", ".ps1", ".psm1", ".bat", ".cmd", ".sh", ".js", ".mjs", ".cjs", ".ts")
#: Full-line comment openers per suffix; a line starting with one is prose.
_COMMENT_OPENERS: dict[str, tuple[str, ...]] = {
    ".py": ("#",), ".sh": ("#",), ".ps1": ("#", "<#"), ".psm1": ("#", "<#"),
    ".bat": ("rem ", "@rem ", "::"), ".cmd": ("rem ", "@rem ", "::"),
    ".js": ("//", "/*", "*"), ".mjs": ("//", "/*", "*"), ".cjs": ("//", "/*", "*"), ".ts": ("//", "/*", "*"),
}
#: Block comments that open a line; every line until the closer is prose.
_BLOCK_COMMENTS: dict[str, tuple[str, str]] = {
    ".ps1": ("<#", "#>"), ".psm1": ("<#", "#>"),
    ".js": ("/*", "*/"), ".mjs": ("/*", "*/"), ".cjs": ("/*", "*/"), ".ts": ("/*", "*/"),
}
_PORT = r"(\d{2,5})(?![\d.])"
# A host followed by `:port`: loopback, any-address, an IPv4, a tailnet name, or an
# interpolated host (`{host}`, `${host}`, `%HOST%`) whose port is still a literal.
_HOST_PORT = re.compile(
    r"(?:(?i:\blocalhost|(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}|\[::1?\]|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net)"
    r"|\}|%)\s*:\s*" + _PORT)
# A port name given a number: `PORT=`, `--port 8130`, `DEFAULT_PORT = `, `-Ports "…"`, `"port": `,
# `port: int = `, camelCase `webappPort = `. `report` / `export` are not names ending in `port`.
_NAMED_PORT = re.compile(
    r"(?:(?i:(?<![a-z])port)|(?<=[a-z])Port)s?\b['\"]?\s*(?::\s*int\s*)?(?:=|:|\s)\s*['\"]?" + _PORT)
_PRIVATE_V4 = re.compile(
    r"(?<![\w.])(10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))"
    r"\.(\d{1,3})\.(\d{1,3})(?![\w.]|/\d)")
_TAILNET_NAME = re.compile(r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b")
_EXEMPT = re.compile(r"port-host-exempt\b:?(.*)")


class ConfigError(RuntimeError):
    """The adopter's configuration or baseline is unusable (exit 2)."""


class NotConfirmed(RuntimeError):
    """The tree could not be read through (exit 3)."""


@dataclass(frozen=True)
class Config:
    baseline: str
    rules: tuple[str, ...]
    resolvers: tuple[str, ...] = ()
    include_tests: bool = False
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    message: str


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if name not in table:
        return default
    value = table[name]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[port_host_ratchet] {name} must be a list of non-empty strings")
    return tuple(v.replace("\\", "/") for v in value)


def parse_config(toml_text: str) -> Config | None:
    """`[port_host_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip. Anything
    malformed raises `ConfigError`: a typo'd key or rule must not quietly run
    with less than the adopter asked for.
    """
    table = tomllib.loads(toml_text).get("port_host_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[port_host_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[port_host_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[port_host_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = _baseline.configured(table.get("baseline", DEFAULT_BASELINE), DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[port_host_ratchet] baseline must be a non-empty string")
    include_tests = table.get("include_tests", False)
    if not isinstance(include_tests, bool):
        raise ConfigError("[port_host_ratchet] include_tests must be true or false")
    rules = _str_list(table, "rules", RULES)
    bad = sorted(set(rules) - set(RULES))
    if not rules or bad:
        raise ConfigError(f"[port_host_ratchet] rules must name rules from: {', '.join(RULES)}"
                          + (f" (unknown: {', '.join(bad)})" if bad else ""))
    return Config(baseline, tuple(r for r in RULES if r in rules), _str_list(table, "resolvers", ()),
                  include_tests, _str_list(table, "exclude", ()))


def vendored_dests(toml_text: str) -> tuple[str, ...]:
    table = tomllib.loads(toml_text).get("vendored")
    found = []
    for entry in (table.values() if isinstance(table, dict) else ()):
        value = entry.get("dest") if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip("/"):
            found.append(value.replace("\\", "/").strip("/"))
    return tuple(sorted(found))


def load_config(root: Path) -> tuple[Config | None, tuple[str, ...]]:
    """(config or None when not enabled, `[vendored]` dests)."""
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_config(text), vendored_dests(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# ------------------------------------------------------------------- files

def _glob_regex(glob: str) -> re.Pattern[str]:
    """Repo-root-anchored glob -> regex; `**/` is zero or more directories."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(text: str, globs: tuple[str, ...]) -> bool:
    return any(_glob_regex(g).match(text) for g in globs)


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.strip("/")
    return path == prefix or path.startswith(prefix + "/")


def is_sample(path: str) -> bool:
    """`config.sample.json`, `ports.example.ps1`, `x.py.sample`: a tracked default, not a running value."""
    name = PurePosixPath(path).name.lower()
    return ".sample" in name or ".example" in name


def is_test(path: str) -> bool:
    """Under a `tests/` folder, or named like a pytest or JS test module."""
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return ("tests" in parts[:-1] or name.startswith("test_") or name.endswith(("_test.py", ".test.js", ".spec.js",
                                                                                ".test.ts", ".spec.ts")))


def in_scope(path: str, cfg: Config, vendored: tuple[str, ...]) -> bool:
    """A script or code file the repo runs, outside its resolver, samples, vendored copies and excludes."""
    if not path.lower().endswith(CODE_SUFFIXES) or is_sample(path):
        return False
    if not cfg.include_tests and is_test(path):
        return False
    if any(_under(path, d) for d in vendored):
        return False
    return not matches_any(path, BUILTIN_EXCLUDES + cfg.exclude + cfg.resolvers)


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # a read-only sweep must not leave index locks
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, env=env, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise NotConfirmed(f"git could not start: {exc}") from exc


def tracked_files(root: Path) -> list[str]:
    """Repo-relative, forward-slash paths of every tracked or untracked-but-not-ignored file."""
    args = ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    proc = _git(root, args)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git {' '.join(args)} exited {proc.returncode}: {detail[-1] if detail else '(no output)'}")
    return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)


def _read_text(root: Path, path: str) -> str | None:
    """A file's text, None when it is tracked but deleted in the working tree."""
    try:
        return (root / path).read_bytes().decode("utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NotConfirmed(f"{path}: cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise NotConfirmed(f"{path}: is not UTF-8 (byte {exc.start})") from exc


# -------------------------------------------------------------------- scan

def docstring_lines(text: str, path: str) -> set[int]:
    """Every line a Python docstring spans (module, class, function); raises `NotConfirmed` on a parse error."""
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError) as exc:
        raise NotConfirmed(f"{path}: cannot be parsed: {exc}") from exc
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def _is_comment(line: str, suffix: str) -> bool:
    stripped = line.lstrip().lower()
    return stripped.startswith(_COMMENT_OPENERS.get(suffix, ()))


def block_comment_lines(lines: list[str], suffix: str) -> set[int]:
    """1-based lines inside a PowerShell `<# ... #>` or JS `/* ... */` block that opens a line (help text, prose)."""
    delimiters = _BLOCK_COMMENTS.get(suffix)
    if delimiters is None:
        return set()
    opener, closer = delimiters
    inside: set[int] = set()
    open_block = False
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not open_block and stripped.startswith(opener):
            open_block = closer not in stripped[len(opener):]
            inside.add(number)
        elif open_block:
            inside.add(number)
            open_block = closer not in stripped
    return inside


def _valid_port(text: str) -> bool:
    return 1 <= int(text) <= 65535


def line_findings(line: str) -> list[tuple[str, str]]:
    """(rule, message) for each port or host literal on one line; one finding per rule."""
    found: list[tuple[str, str]] = []
    port = next((m for rx in (_HOST_PORT, _NAMED_PORT) for m in rx.finditer(line) if _valid_port(m.group(1))), None)
    if port is not None:
        found.append(("port-literal", f"port {port.group(1)} written in code: `{port.group(0).strip()}`"))
    host = next((m.group(0) for m in _PRIVATE_V4.finditer(line)
                 if all(int(octet) <= 255 for octet in m.group(0).split("."))), None)
    if host is None:
        tailnet = _TAILNET_NAME.search(line)
        host = tailnet.group(0) if tailnet else None
    if host is not None:
        found.append(("host-literal", f"machine-local host written in code: `{host}`"))
    return found


def _exemption(lines: list[str], index: int, suffix: str) -> str | None:
    """The marker's reason ('' when bare) on line `index` or the comment line above; None when unmarked."""
    for probe in (index, index - 1):
        if probe < 0 or (probe != index and not _is_comment(lines[probe], suffix)):
            continue
        match = _EXEMPT.search(lines[probe])
        if match:
            return match.group(1).strip()
    return None


@dataclass
class FileScan:
    findings: list[tuple[int, str, str]] = field(default_factory=list)  # (line, rule, message)
    exempted: list[tuple[int, str, str]] = field(default_factory=list)  # (line, rule, reason)


def scan_source(text: str, path: str) -> FileScan:
    """Port and host literals in one file's text, its comments and docstrings skipped."""
    suffix = PurePosixPath(path).suffix.lower()
    lines = text.splitlines()
    skip = docstring_lines(text, path) if suffix == ".py" else block_comment_lines(lines, suffix)
    result = FileScan()
    for index, line in enumerate(lines):
        number = index + 1
        if number in skip or _is_comment(line, suffix):
            continue
        found = line_findings(line)
        if not found:
            continue
        reason = _exemption(lines, index, suffix)
        if reason is None:
            result.findings += [(number, rule, msg) for rule, msg in found]
        elif not reason:
            result.findings.append((number, "bare-exempt", "`port-host-exempt` needs a reason after the colon"))
            result.findings += [(number, rule, msg) for rule, msg in found]
        else:
            result.exempted += [(number, rule, reason) for rule, _ in found]
    return result


@dataclass
class Tree:
    found: dict[str, list[Hit]] = field(default_factory=dict)
    exempted: list[tuple[str, int, str, str]] = field(default_factory=list)  # (path, line, rule, reason)
    scanned: int = 0


def scan_tree(root: Path, cfg: Config, vendored: tuple[str, ...]) -> Tree:
    tree = Tree()
    for path in tracked_files(root):
        if not in_scope(path, cfg, vendored):
            continue
        text = _read_text(root, path)
        if text is None:
            continue
        tree.scanned += 1
        scanned = scan_source(text, path)
        tree.exempted += [(path, line, rule, reason) for line, rule, reason in scanned.exempted]
        hits = [Hit(path, line, rule, msg) for line, rule, msg in scanned.findings if rule in cfg.rules]
        if hits:
            tree.found[path] = sorted(hits, key=lambda h: (h.line, h.rule))
    return tree


# ---------------------------------------------------------------- baseline

Counts = dict[str, Counter[str]]  # path -> Counter(rule)


def current_counts(found: dict[str, list[Hit]]) -> Counts:
    return {path: Counter(h.rule for h in hits) for path, hits in found.items()}


def read_baseline(path: Path) -> Counts:
    """Baseline file -> ``{file path: Counter(rule)}``. Raises `ConfigError` on anything off."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read baseline {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise ConfigError(f"baseline {path}: expected an object with version {BASELINE_VERSION}")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise ConfigError(f"baseline {path}: `entries` must be a list")
    out: Counts = {}
    for item in raw_entries:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ConfigError(f"baseline {path}: malformed entry {item!r}")
        counts = item.get("counts")
        if (not isinstance(counts, dict) or not counts
                or not all(r in RULES and isinstance(n, int) and not isinstance(n, bool) and n > 0
                           for r, n in counts.items())):
            raise ConfigError(f"baseline {path}: entry {item!r} needs `counts` of rules to positive integers")
        out[item["key"]] = Counter(counts)
    return out


def render_baseline(counts: Counts) -> str:
    """Sorted, one file per line, no line numbers, so a baseline diff reviews cleanly."""
    lines = []
    for key in sorted(counts):
        record = {"key": key, "counts": {r: counts[key][r] for r in RULES if counts[key][r]}}
        lines.append("    " + json.dumps(record, ensure_ascii=False))
    body = "[\n" + ",\n".join(lines) + "\n  ]" if lines else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "entries": {body}\n}}\n'


def diff(current: Counts, baseline: Counts) -> tuple[Counts, int]:
    """(per file, the rule counts beyond the baseline; how many baselined hits are gone)."""
    added: Counts = {}
    gone = 0
    for path in sorted(set(current) | set(baseline)):
        cur, base = current.get(path, Counter()), baseline.get(path, Counter())
        extra = cur - base
        if extra:
            added[path] = extra
        gone += sum((base - cur).values())
    return added, gone


# -------------------------------------------------------------------- CLI

def _total(counts: Counts) -> int:
    return sum(sum(c.values()) for c in counts.values())


def _report_added(added: Counts, found: dict[str, list[Hit]], baseline: Counts) -> None:
    print(f"port host ratchet: {_total(added)} hit(s) beyond the baseline, in {len(added)} file(s):")
    for path, extra in added.items():
        for rule in RULES:
            if not extra[rule]:
                continue
            had = baseline.get(path, Counter())[rule]
            print(f"  {path}: [{rule}] {had + extra[rule]} hit(s), baseline {had}:")
            for h in found[path]:
                if h.rule == rule:
                    print(f"    {path}:{h.line}: [{rule}] {h.message}")


def _report_context(tree: Tree, cfg: Config) -> None:
    resolvers = ", ".join(cfg.resolvers) if cfg.resolvers else "none declared"
    print(f"  {tree.scanned} file(s) read; resolvers: {resolvers}; {len(tree.exempted)} exemption(s)"
          + (":" if tree.exempted else ""))
    for path, line, rule, reason in sorted(tree.exempted):
        print(f"    {path}:{line}: [{rule}] {reason}")


def _check(cfg: Config, tree: Tree, current: Counts, baseline: Counts) -> int:
    added, gone = diff(current, baseline)
    if added:
        _report_added(added, tree.found, baseline)
        print("  Read the value from the repo's config resolver (an ignored config file with a tracked sample "
              "default) instead of writing it here, so a machine never edits a tracked file to run the app "
              "(docs/port-host-ratchet.md). A hit that must stay carries `port-host-exempt: <reason>`; or "
              "(reviewed) raise the baseline with `--update-baseline --allow-additions`.")
        _report_context(tree, cfg)
        return EXIT_RISE
    print(f"port host ratchet: PASS ({_total(current)} baselined hit(s) in {len(current)} file(s), none new)")
    _report_context(tree, cfg)
    if gone:
        print(f"  {gone} baselined hit(s) are gone -- run "
              "`python scripts/port_host_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def _load_baseline(baseline_path: Path, cfg: Config, init: bool) -> Counts | int:
    """The committed baseline (empty for `--init`), or the exit code that refuses the run."""
    if init:
        if baseline_path.exists():
            print(f"port host ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return {}
    if not baseline_path.is_file():
        print(f"port host ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/port_host_ratchet.py --init`")
        return EXIT_RISE
    try:
        return read_baseline(baseline_path)
    except ConfigError as exc:
        print(f"port host ratchet: BASELINE ERROR: {exc}")
        return EXIT_CONFIG


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg, vendored = load_config(root)
    except ConfigError as exc:
        print(f"port host ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("port host ratchet: not enabled -- set [port_host_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("port host ratchet: not enabled (no [port_host_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = _baseline.resolve(root, cfg.baseline, DEFAULT_BASELINE, "port host ratchet")
    if baseline_path is None:
        return EXIT_CONFIG
    baseline = _load_baseline(baseline_path, cfg, init)
    if isinstance(baseline, int):
        return baseline
    try:
        tree = scan_tree(root, cfg, vendored)
    except NotConfirmed as exc:
        print(f"port host ratchet: NOT CONFIRMED: {exc}")
        print("  (the tree could not be read through, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    current = current_counts(tree.found)

    if init:
        # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"port host ratchet: wrote {cfg.baseline} with {_total(current)} hit(s) in {len(current)} file(s)")
        return EXIT_PASS
    if not update:
        return _check(cfg, tree, current, baseline)
    added, gone = diff(current, baseline)
    if added and not allow_additions:
        _report_added(added, tree.found, baseline)
        print("port host ratchet: nothing written -- move the values into config, or raise the baseline with "
              "--allow-additions")
        return EXIT_RISE
    # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
    baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
    print(f"port host ratchet: {cfg.baseline} now has {_total(current)} hit(s) in {len(current)} file(s) "
          f"({gone} dropped, {_total(added)} added)")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="drop hits now gone (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also add hits (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    return run(args.root.resolve(), init=args.init, update=args.update_baseline,
               allow_additions=args.allow_additions)


if __name__ == "__main__":
    # Reports contain non-ASCII; a cp1252 console would otherwise crash after the verdict.
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # real console streams are TextIOWrapper
    sys.exit(main())
