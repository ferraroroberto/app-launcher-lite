"""Ratchet on what a start smoke writes into the work tree: the set of paths may only shrink.

Updating a fleet app is a clean `git pull --ff-only` plus its restart recipe,
and starting, restarting and gating a repo leaves `git status` unchanged:
runtime output goes to ignored state dirs. This stage records
`git status --porcelain` (with each listed file's content hash), runs the
repo's declared start smoke -- a short, disposable boot -- records the status
again, and fails when the smoke added, modified or deleted a path that the
committed baseline does not list.

The rule (docs/clean-tree-ratchet.md):

  * `tree-write` -- a path whose `git status` entry or content differs after
    the smoke: a new untracked file, a tracked file modified or deleted, a
    file already dirty before the run that the smoke changed again or
    reverted. Ignored files are never counted.

The smoke is an argv list the repo declares. Three placeholders are filled in
each run: `{python}` (this interpreter), `{port}` (a free loopback port picked
for the run) and `{data_dir}` (a fresh temporary folder, also exported under
`data_dir_env` when that is set), so the smoke never shares a port or state
with a live instance. With `ready_path` the smoke is a server: it is started,
`GET http://127.0.0.1:{port}<ready_path>` is polled until it answers 200, the
server runs `settle_s` more seconds and its process tree is stopped. Without
`ready_path` the smoke runs to completion and must exit 0.

Opt-in, per adopter, in its own `.fleet.toml`:

    [clean_tree_ratchet]
    enabled      = true
    baseline     = ".ratchets/clean-tree.json"  # default
    smoke        = ["{python}", "-m", "myapp", "--port", "{port}"]  # required
    ready_path   = "/healthz"         # optional; absent = run to completion
    timeout_s    = 60                 # default; start (or run) deadline
    settle_s     = 2                  # default; seconds kept running after ready
    data_dir_env = "MYAPP_DATA_DIR"   # optional; set to `{data_dir}`

Exit codes: 0 pass or not enabled; 1 a path beyond the baseline was written,
or the baseline is missing / a mode was refused; 2 bad configuration, baseline
or arguments; 3 NOT CONFIRMED (`git` unusable, a listed file that cannot be
read, a smoke that does not start, exits non-zero, dies or is not ready in
time) -- the gate fails on it. Modes: `check`, `--init`, `--update-baseline`
(shrink only), `--update-baseline --allow-additions` (shows in review). Every
mode runs the smoke.

Vendor-verbatim (manifest key `clean_tree_ratchet`), stdlib-only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
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
        print(f"clean tree ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor both "
              "(manifest keys clean_tree_ratchet and ratchet_baseline)")
        sys.exit(3)


_baseline = _load_sibling("ratchet_baseline")  # where the baseline lives, and its move into .ratchets/

REPO_ROOT = Path(__file__).resolve().parent.parent

RULES: tuple[str, ...] = ("tree-write",)
BASELINE_VERSION = 1
DEFAULT_BASELINE = ".ratchets/clean-tree.json"
DEFAULT_TIMEOUT_S = 60
DEFAULT_SETTLE_S = 2.0
GIT_TIMEOUT_S = 120
STOP_TIMEOUT_S = 30
# A smoke's ready probe is a bounded start-up wait, not a poll loop of a running app.
READY_INTERVAL_S = 0.5
LOG_TAIL_LINES = 20
STATE_NAME = "clean-tree-ratchet-flagged.json"
EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "smoke", "ready_path", "timeout_s", "settle_s", "data_dir_env"}
_PLACEHOLDERS = ("{python}", "{port}", "{data_dir}")


class ConfigError(RuntimeError):
    """The adopter's configuration or baseline is unusable (exit 2)."""


class NotConfirmed(RuntimeError):
    """The tree or the smoke could not be observed through (exit 3)."""


@dataclass(frozen=True)
class Config:
    baseline: str
    smoke: tuple[str, ...]
    ready_path: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    settle_s: float = DEFAULT_SETTLE_S
    data_dir_env: str | None = None


# ----------------------------------------------------------------- config

def _number(table: dict[str, object], name: str, default: float, minimum: float) -> float:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"[clean_tree_ratchet] {name} must be a number >= {minimum:g}")
    return float(value)


def _optional_str(table: dict[str, object], name: str) -> str | None:
    value = table.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[clean_tree_ratchet] {name} must be a non-empty string")
    return value


def parse_config(toml_text: str) -> Config | None:
    """`[clean_tree_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip. Anything
    malformed raises `ConfigError`: an enabled check with no smoke to run
    must not pass as a clean tree.
    """
    table = tomllib.loads(toml_text).get("clean_tree_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[clean_tree_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[clean_tree_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[clean_tree_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = _baseline.configured(table.get("baseline", DEFAULT_BASELINE), DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[clean_tree_ratchet] baseline must be a non-empty string")
    smoke = table.get("smoke")
    if not isinstance(smoke, list) or not smoke or not all(isinstance(a, str) and a for a in smoke):
        raise ConfigError("[clean_tree_ratchet] smoke must be a non-empty list of non-empty strings "
                          "(the argv of the repo's disposable start smoke)")
    ready_path = _optional_str(table, "ready_path")
    if ready_path is not None and not ready_path.startswith("/"):
        raise ConfigError("[clean_tree_ratchet] ready_path must start with '/'")
    if ready_path is not None and not any("{port}" in a for a in smoke):
        raise ConfigError("[clean_tree_ratchet] a smoke with ready_path must take its port from `{port}`")
    return Config(baseline, tuple(smoke), ready_path,
                  _number(table, "timeout_s", DEFAULT_TIMEOUT_S, 1),
                  _number(table, "settle_s", DEFAULT_SETTLE_S, 0),
                  _optional_str(table, "data_dir_env"))


def load_config(root: Path) -> Config | None:
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_config(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# ------------------------------------------------------------------- tree

Snapshot = dict[str, tuple[str, str]]  # path -> (porcelain status, content hash or "-")


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # a read-only status must not leave index locks
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, env=env, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise NotConfirmed(f"git could not start: {exc}") from exc


def parse_porcelain(raw: bytes) -> dict[str, str]:
    """`git status --porcelain=v1 -z` -> {path: two-letter status}; a rename's source is listed too."""
    fields = raw.decode("utf-8", "replace").split("\0")
    out: dict[str, str] = {}
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        out[path] = status
        if status[0] in "RC" or status[1] in "RC":
            if i < len(fields) and fields[i]:
                out[fields[i]] = status + "<"  # the source side of a rename or copy
            i += 1
    return out


def _content_hash(root: Path, path: str) -> str:
    target = root / path
    if not target.is_file():
        return "-"
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError as exc:
        raise NotConfirmed(f"{path}: cannot be read: {exc}") from exc


def snapshot(root: Path) -> Snapshot:
    """Every path `git status` lists (untracked files one by one), with its content hash."""
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    proc = _git(root, args)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git {' '.join(args)} exited {proc.returncode}: {detail[-1] if detail else '(no output)'}")
    return {path: (status, _content_hash(root, path)) for path, status in parse_porcelain(proc.stdout).items()}


def _state_path(root: Path) -> Path:
    """Where a failed run records what it flagged: inside the git dir (per worktree), never the work tree."""
    proc = _git(root, ["rev-parse", "--git-path", STATE_NAME])
    if proc.returncode != 0:
        raise NotConfirmed(f"git rev-parse --git-path exited {proc.returncode}")
    path = Path(proc.stdout.decode("utf-8", "replace").strip())
    return path if path.is_absolute() else root / path


def read_state(path: Path) -> Snapshot:
    """The paths the last failed run flagged, each as the smoke left it; {} when none or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {p: (v[0], v[1]) for p, v in data.items()
            if isinstance(p, str) and isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) for x in v)}


def write_state(path: Path, flagged: Snapshot) -> None:
    if not flagged:
        path.unlink(missing_ok=True)
        return
    # runtime-write-exempt: inside the repo's git dir, never a code folder
    path.write_text(json.dumps({p: list(v) for p, v in sorted(flagged.items())}, indent=1), encoding="utf-8")


def carried_paths(state: Snapshot, before: Snapshot, after: Snapshot) -> dict[str, str]:
    """Paths a previous run flagged that still sit exactly as that smoke left them.

    A smoke that rewrites the same bytes leaves no trace the second time, so without this
    a re-run of a red gate would pass over the write nobody reverted.
    """
    return {p: f"{entry[0].strip() or 'clean'}, left by an earlier smoke and not reverted"
            for p, entry in state.items() if before.get(p) == entry and after.get(p) == entry}


def written_paths(before: Snapshot, after: Snapshot) -> dict[str, str]:
    """{path: 'before -> after'} for every path whose status or content the run changed."""
    out: dict[str, str] = {}
    for path in sorted(set(before) | set(after)):
        was, now = before.get(path), after.get(path)
        if was == now:
            continue
        was_s = was[0].strip() or "clean" if was else "clean"
        now_s = now[0].strip() or "clean" if now else "clean"
        if was_s == now_s:
            now_s += " (content changed)"
        out[path] = f"{was_s} -> {now_s}"
    return out


# ------------------------------------------------------------------ smoke

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def expand(argv: tuple[str, ...], port: int, data_dir: str) -> list[str]:
    values = {"{python}": sys.executable, "{port}": str(port), "{data_dir}": data_dir}
    out = []
    for arg in argv:
        for key in _PLACEHOLDERS:
            arg = arg.replace(key, values[key])
        out.append(arg)
    return out


def _ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 -- loopback URL this stage built
            return int(resp.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _stop_tree(proc: subprocess.Popen[bytes]) -> None:
    """Stop the smoke and every child it started; a re-exec'ing venv launcher leaves the server as a child."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=STOP_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=STOP_TIMEOUT_S / 3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"the smoke (pid {proc.pid}) did not stop within {STOP_TIMEOUT_S}s") from exc


def _tail(log_path: Path) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "    (no smoke output could be read)"
    return "\n".join("    " + line for line in lines[-LOG_TAIL_LINES:]) or "    (the smoke printed nothing)"


def _wait_ready(proc: subprocess.Popen[bytes], url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not _ready(url):
        code = proc.poll()
        if code is not None:
            raise NotConfirmed(f"the smoke exited {code} before {url} answered 200")
        if time.monotonic() > deadline:
            raise NotConfirmed(f"{url} did not answer 200 within {timeout_s:g}s")
        time.sleep(READY_INTERVAL_S)


def run_smoke(root: Path, cfg: Config) -> str:
    """Run the declared smoke once against a disposable port and data dir; a one-line summary.

    Raises `NotConfirmed` when it does not start, is not ready, dies or exits non-zero:
    a smoke that never ran cannot show that the tree stays clean.
    """
    with tempfile.TemporaryDirectory(prefix="clean-tree-smoke-") as scratch:
        data_dir = Path(scratch) / "data"
        # runtime-write-exempt: inside the run's own TemporaryDirectory, removed on exit
        data_dir.mkdir()
        log_path = Path(scratch) / "smoke.log"
        port = free_port()
        argv = expand(cfg.smoke, port, str(data_dir))
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        if cfg.data_dir_env:
            env[cfg.data_dir_env] = str(data_dir)
        started = time.monotonic()
        # runtime-write-exempt: inside the run's own TemporaryDirectory, removed on exit
        with log_path.open("wb") as log:
            try:
                proc = subprocess.Popen(  # noqa: S603 -- the argv the repo itself declares
                    argv, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=_NO_WINDOW, start_new_session=sys.platform != "win32",
                )
            except OSError as exc:
                raise NotConfirmed(f"the smoke could not start: {exc}") from exc
            try:
                if cfg.ready_path is not None:
                    url = f"http://127.0.0.1:{port}{cfg.ready_path}"
                    _wait_ready(proc, url, cfg.timeout_s)
                    time.sleep(cfg.settle_s)
                    if proc.poll() is not None:
                        raise NotConfirmed(f"the smoke exited {proc.returncode} after it was ready")
                    summary = f"server ready at {cfg.ready_path} in {time.monotonic() - started:.1f}s, then stopped"
                else:
                    try:
                        code = proc.wait(timeout=cfg.timeout_s)
                    except subprocess.TimeoutExpired as exc:
                        raise NotConfirmed(f"the smoke did not finish within {cfg.timeout_s:g}s") from exc
                    if code != 0:
                        raise NotConfirmed(f"the smoke exited {code}")
                    time.sleep(cfg.settle_s)
                    summary = f"ran to completion in {time.monotonic() - started:.1f}s"
            except NotConfirmed as exc:
                _stop_tree(proc)
                log.flush()
                raise NotConfirmed(f"{exc}; last smoke output:\n{_tail(log_path)}") from exc
            _stop_tree(proc)
        return summary


# ---------------------------------------------------------------- baseline

def read_baseline(path: Path) -> list[str]:
    """Baseline file -> sorted list of paths the smoke is known to write. Raises `ConfigError` on anything off."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read baseline {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise ConfigError(f"baseline {path}: expected an object with version {BASELINE_VERSION}")
    paths = data.get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise ConfigError(f"baseline {path}: `paths` must be a list of non-empty strings")
    return sorted(set(paths))


def render_baseline(paths: list[str]) -> str:
    """Sorted, one path per line, so a baseline diff reviews cleanly."""
    lines = ",\n".join("    " + json.dumps(p, ensure_ascii=False) for p in sorted(set(paths)))
    body = "[\n" + lines + "\n  ]" if lines else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "paths": {body}\n}}\n'


# -------------------------------------------------------------------- CLI

def _report_added(added: list[str], written: dict[str, str]) -> None:
    print(f"clean tree ratchet: the smoke wrote {len(added)} path(s) beyond the baseline:")
    for path in added:
        print(f"  {path}: [tree-write] {written[path]}")


def _check(written: dict[str, str], baseline: list[str], summary: str) -> int:
    added = [p for p in written if p not in baseline]
    gone = [p for p in baseline if p not in written]
    if added:
        _report_added(added, written)
        print("  A start or restart must leave `git status` unchanged: send runtime output to an ignored "
              "state dir (or the runtime data root), never into a tracked file or an unignored path "
              "(docs/clean-tree-ratchet.md). Or (reviewed) raise the baseline with "
              "`--update-baseline --allow-additions`.")
        print(f"  smoke: {summary}")
        return EXIT_RISE
    print(f"clean tree ratchet: PASS ({len(written)} baselined path(s) written, none new; smoke {summary})")
    if gone:
        print(f"  {len(gone)} baselined path(s) were not written this run -- run "
              "`python scripts/clean_tree_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def _load_baseline(baseline_path: Path, cfg: Config, init: bool) -> list[str] | int:
    """The committed baseline (empty for `--init`), or the exit code that refuses the run."""
    if init:
        if baseline_path.exists():
            print(f"clean tree ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return []
    if not baseline_path.is_file():
        print(f"clean tree ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/clean_tree_ratchet.py --init`")
        return EXIT_RISE
    try:
        return read_baseline(baseline_path)
    except ConfigError as exc:
        print(f"clean tree ratchet: BASELINE ERROR: {exc}")
        return EXIT_CONFIG


def _remember(state_path: Path, flagged: Snapshot) -> None:
    try:
        write_state(state_path, flagged)
    except OSError as exc:
        print(f"clean tree ratchet: WARN: could not record the flagged paths in {state_path}: {exc}; "
              "a re-run will not see a write the smoke repeats byte for byte")


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg = load_config(root)
    except ConfigError as exc:
        print(f"clean tree ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("clean tree ratchet: not enabled -- set [clean_tree_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("clean tree ratchet: not enabled (no [clean_tree_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = _baseline.resolve(root, cfg.baseline, DEFAULT_BASELINE, "clean tree ratchet")
    if baseline_path is None:
        return EXIT_CONFIG
    baseline = _load_baseline(baseline_path, cfg, init)
    if isinstance(baseline, int):
        return baseline
    try:
        state_path = _state_path(root)
        before = snapshot(root)
        summary = run_smoke(root, cfg)
        after = snapshot(root)
    except NotConfirmed as exc:
        print(f"clean tree ratchet: NOT CONFIRMED: {exc}")
        print("  (the smoke or the tree could not be observed, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    written = written_paths(before, after)
    for path, why in carried_paths(read_state(state_path), before, after).items():
        written.setdefault(path, why)
    added = [p for p in written if p not in baseline]
    refused = (not init and not update and added) or (update and added and not allow_additions)
    _remember(state_path, {p: after[p] for p in added if p in after} if refused else {})

    if init:
        # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
        baseline_path.write_text(render_baseline(list(written)), encoding="utf-8", newline="\n")
        print(f"clean tree ratchet: wrote {cfg.baseline} with {len(written)} path(s); smoke {summary}")
        return EXIT_PASS
    if not update:
        return _check(written, baseline, summary)
    if added and not allow_additions:
        _report_added(added, written)
        print("clean tree ratchet: nothing written -- move the output to an ignored path, or raise the baseline "
              "with --allow-additions")
        return EXIT_RISE
    kept = sorted(set(written))
    # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
    baseline_path.write_text(render_baseline(kept), encoding="utf-8", newline="\n")
    print(f"clean tree ratchet: {cfg.baseline} now has {len(kept)} path(s) "
          f"({len(set(baseline) - set(kept))} dropped, {len(added)} added)")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="drop paths no longer written (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also add paths (shows in review)")
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
