"""Guard against a README bullet ballooning into a run-on paragraph.

app-launcher-lite#131: the "Verifying changes" gate bullet grew into one
~2,800-character paragraph that restated every ratchet's rules already given,
bullet by bullet, in CLAUDE.md -- and a splice along the way left a broken
sentence in it ("It boots The last stage is..."). The global CLAUDE.md rule:
"The README is an index ... under 40 KiB; reference moves to `docs/<topic>.md`".
A single bullet this long is both the duplication and the hardest place in the
file to notice a dropped or spliced clause, so it is checked structurally
rather than relying on the next audit pass to read it closely.

This repo's own README already carries detailed per-stage bullets (the ports
table footnotes, the screen-view bullets) up to ~580 characters with no
duplication problem, so the cap sits well above normal prose and well below
the ~2,800-character bullet that prompted this check -- a bullet this long is
restating prose that belongs in CLAUDE.md or `docs/`, not writing README prose.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# 2x the longest legitimate bullet in this README today (581 chars); the
# bullet that prompted this check was 3018 chars.
MAX_BULLET_CHARS = 1200


def _top_level_bullets(text: str) -> list[str]:
    """Top-level markdown bullets (`- ...` with no leading indent).

    Indented sub-bullets are a different concern (nesting, not run-on prose)
    and are left out on purpose.
    """
    return [line for line in text.split("\n") if line.startswith("- ")]


def test_no_readme_bullet_is_a_run_on_paragraph() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    offenders = [
        (len(bullet), bullet[:70]) for bullet in _top_level_bullets(text) if len(bullet) > MAX_BULLET_CHARS
    ]
    assert not offenders, (
        f"README bullet(s) over {MAX_BULLET_CHARS} chars -- likely restating "
        f"CLAUDE.md prose instead of pointing to it: {offenders}"
    )
