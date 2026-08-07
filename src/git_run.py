"""The one ``git`` invocation contract for this repo (issue #11).

Every ``git -C <root> …`` shell-out here needs the same kwargs — capture the
output, close stdin so a git that wants to prompt fails instead of hanging,
decode as text, cap the wall clock, never raise on a non-zero exit, and pass
``creationflags=NO_WINDOW`` so no console flashes under the console-less
``pythonw`` tray — plus the same ``except (OSError, subprocess.SubprocessError)``
arm around it. That contract used to be hand-typed at five separate call sites
across :mod:`src.build_info`, :mod:`src.session_host_paths` and
:mod:`src.scanner`, and the copies had already drifted: structurally identical
blocks disagreed on whether a failed run warned or degraded silently. Named
once here, changing the timeout or the exception set is a one-line edit rather
than a five-file sweep that misses one.

The split of responsibility is deliberate:

* **This module** owns *running* git. It returns ``None`` only when the
  process never produced a result at all (git missing from PATH, a timeout, an
  OS-level spawn failure) and logs that once, with the command and the root.
* **Call sites** own *interpreting* the result — a non-zero ``returncode`` is
  an answer (not a repo, unknown ref, unresolvable sha), not a failure to run,
  so it comes back as a normal :class:`subprocess.CompletedProcess` for the
  caller to read however its own domain requires.

``None`` therefore means "couldn't determine", and callers must degrade to
their own unknown value — never to a confident negative.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Union

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

#: Default wall-clock cap. Every consumer but :mod:`src.scanner` (which walks
#: whole project directories and allows itself more headroom) runs with this.
DEFAULT_TIMEOUT_S = 5.0


def run_git(
    project_root: Union[Path, str],
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Optional[subprocess.CompletedProcess]:
    """Run ``git -C <project_root> <args>`` under the shared contract.

    Returns the completed process regardless of its exit code, or ``None``
    when git could not be run at all (see the module docstring). Never
    raises.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "⚠️ git %s in %s could not run: %s: %s",
            " ".join(args), project_root, type(exc).__name__, exc,
        )
        return None
