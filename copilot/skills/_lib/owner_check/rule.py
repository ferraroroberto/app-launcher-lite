"""The deterministic owner-check rule: does this work need Roberto, and why.

Pure: no `gh`, no git, no clock. The inputs are the issue's type labels, its title,
its body (only for the explicit `Owner validation:` line), the PR's changed files
and what the lane says about in-session verification. The rule only ever adds
reasons; an explicit `none` in the issue cannot suppress one, so a drafting slip
never hides real owner work.

Self-verification comes first. Whatever the lane proved in its own session needs
nobody. What it could not prove needs Roberto only through a named criterion and
one owner-only class (`OWNER_CLASSES`); a generic "could not verify" is no reason.
A live-only title word asks him only while no in-session proof exists, and a
live-input path in the diff always does. A new page or view is browser-provable, so
it is never his: `unproven_surface` names one no visual proof covered, for the
lane to prove.
An account clause fires only for a third party's account (sign-up, OAuth,
subscription): the app's own password sign-in is drivable with test credentials. Words are matched in the title only: issue bodies quote viewports
("iPhone 390px"), network loopback and "report" so often that body matching
flagged more issues than were enhancements at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

REQUIRED = "required"
NONE = "none"
UNKNOWN = "unknown"

# What the lane says about re-running the original repro / acceptance in-session.
VERIFIED_YES = "yes"
VERIFIED_NO = "no"
# Everything verified in-session except a criterion only a later scheduled run can
# show; reading that run is the chief's job, so it adds no reason.
VERIFIED_SCHEDULED = "scheduled"
# Before the work exists (`/issue-add`) and for history (`backfill`) verification
# is not a fact anyone can state, so the clause does not apply.
VERIFIED_NA = "n/a"
VERIFICATION_STATES = (VERIFIED_YES, VERIFIED_SCHEDULED, VERIFIED_NO, VERIFIED_NA)
_PROVEN = (VERIFIED_YES, VERIFIED_SCHEDULED)

# The only reasons a session cannot prove a criterion itself. Anything else is
# provable with the gate, the repro, a screenshot, rendered geometry or a probe,
# using the tools in `PROOF_TOOLS`. A design choice is a plan-gate question before
# the build, never a validation after it, so there is no taste class.
OWNER_CLASSES = {
    "device": "real phone hardware or a physical sensor no emulation reaches",
    "owner-account": "an action in an account only the owner holds, with real side effects",
    "physical": "a physical action no automation can perform",
    "third-party": "a person other than the owner, or an external service he must sign up for",
}
# Class descriptions recorded before the classes were narrowed, so a re-check can
# still read an older gap back to its class.
_LEGACY_CLASS_TEXT = {
    "a real device or live input (mic, camera, phone hardware)": "device",
    "an account or sign-in only the owner holds": "owner-account",
    "a physical action": "physical",
    "a third-party system the session cannot reach": "third-party",
    "a taste or judgement call": "taste",
}
# What a lane uses before it may declare any gap (listed in docs/owner-check.md).
PROOF_TOOLS = (
    "Playwright Chromium and WebKit with phone profiles",
    "the Mac runner (Xcode, simulator)",
    "screenshots and /design-review",
    "the launcher API for throwaway sessions",
    "the passwordless SSH to satellites",
    "the repo's restart recipe and build-identity check",
    "the local hub's TTS for real speech input",
    "pywinauto/UIA for Windows windows",
)
# `device` is accepted only when the criterion names one of these: the hardware
# behaviour emulation and the Mac runner cannot reach.
_DEVICE_ITEMS = re.compile(
    r"\b(on-screen keyboard|keyboard accessory row|accessory row|microphones?|mic|lock[- ]screen|"
    r"background audio|home[- ]screen|pwa install|haptics?|cameras?|car|physical home devices?|"
    r"home devices?)\b",
    re.IGNORECASE,
)
# A gap criterion naming any of these is lane-provable, whatever its class; each
# pattern names the tool to use instead. Closed list, no fuzzy matching.
_LANE_PROVABLE: Tuple[Tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), tool) for pattern, tool in (
        (r"\b(restart(s|ed|ing)?|re-?build|build|version|sha)\b",
         "the restart recipe and its build-identity check"),
        (r"\b(screenshots?|looks?|layout|light|dark|mock-?ups?)\b",
         "visual proof (a screenshot) and /design-review"),
        (r"\b(desktop|keyboard|keys?|click(s|ed|ing)?|ctrl|enter|escape|esc|drag(s|ged|ging)?)\b",
         "Playwright for the browser, pywinauto/UIA for a Windows window"),
        (r"\b(phone width|iphone layout|touch)\b", "the WebKit phone profile"),
        (r"\b(sessions?|lanes?|launch(es|ed|ing)?)\b", "the launcher API (a throwaway session)"),
        (r"\b(ssh|satellites?)\b", "the passwordless SSH to the satellite"),
        (r"\b(jobs?)\b", "the launcher Jobs API"),
        (r"\b(ci|workflows?)\b", "the local gate; CI being off is a blocked-by, never an owner check"),
        (r"(^|\W)#\d+\b", "a blocked-by on that issue, never an owner check"),
    )
)
# Recorded lane reasons open with this, so a re-check keeps them as recorded.
GAP_PREFIX = "the lane could not prove"

_EXPLICIT = re.compile(r"^\W*owner validation\W*\s*(required|none)\b", re.IGNORECASE | re.MULTILINE)

# Input a session cannot drive: a real device, a live microphone or camera.
_LIVE_ONLY_TITLE = re.compile(
    r"\b(microphones?|mic|webcams?|cameras?|wasapi|dictation|voice notes?|iphone|ipad|android|"
    r"on-device|real device|haptics?|on-screen keyboard|home-screen|bluetooth)\b",
    re.IGNORECASE,
)
_LIVE_ONLY_PATH_TOKENS = frozenset({"wasapi", "mic", "microphone", "camera", "webcam"})
# Docs and tests under a live-input folder change no live input.
_NOT_LIVE_INPUT = re.compile(r"\.md$|(^|/)(docs?|tests?)/", re.IGNORECASE)
# A third party's account only the owner holds (enhancements only). The app's own
# sign-in is not here: a session drives it with test credentials.
_THIRD_PARTY = r"google|microsoft|apple|github|telegram|twitter|linkedin|notion|slack|stripe|cloudflare"
_ACCOUNT_TITLE = re.compile(
    rf"\b(sign[- ]?ups?|oauth|subscriptions?|(?:sign|log)[- ]?in with|"
    rf"(?:{_THIRD_PARTY}) (?:account|sign[- ]?in|log[- ]?in))\b",
    re.IGNORECASE,
)
# A new page or view entry is a surface the lane must visually prove (enhancements
# only); a vendored component, a helper script or a test fixture is not.
_PAGE_PATH = re.compile(r"\.html?$", re.IGNORECASE)
_VIEW_PATH = re.compile(r"(^|/)views?/[^/]+\.m?js$|[-_.]view\.m?js$", re.IGNORECASE)
_NOT_A_SURFACE = re.compile(r"(^|/)(_vendored|tests?|docs?|fixtures?)/", re.IGNORECASE)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


@dataclass(frozen=True)
class Decision:
    verdict: str
    reasons: Tuple[str, ...]


def gap_refusal(cls: str, criterion: str) -> Optional[str]:
    """Why a lane may not hand this gap to the owner, naming the tool to use; None when it holds.

    An allowlisted device item is cut out before the closed refusal list runs, so
    "on-screen keyboard" is not refused as a keyboard check.
    """
    rest = _DEVICE_ITEMS.sub(" ", criterion)
    for pattern, tool in _LANE_PROVABLE:
        m = pattern.search(rest)
        if m:
            return f"{criterion!r} is lane-provable ({m.group(0).strip()}): prove it with {tool}"
    if cls == "device" and not _DEVICE_ITEMS.search(criterion):
        return (f"{criterion!r} names no hardware emulation cannot reach: prove it with emulation "
                "(the WebKit phone profile or the Mac simulator)")
    return None


def parse_gap(text: str) -> Tuple[str, str]:
    """`<class>:<criterion>` -> (class, criterion); ValueError names what is wrong or lane-provable."""
    cls, sep, criterion = (text or "").partition(":")
    cls, criterion = cls.strip().lower(), " ".join(criterion.split())
    if not sep or cls not in OWNER_CLASSES or not criterion:
        raise ValueError(f"expected <class>:<criterion> with class one of {', '.join(OWNER_CLASSES)}, got {text!r}")
    refusal = gap_refusal(cls, criterion)
    if refusal:
        raise ValueError(refusal)
    return cls, criterion


def recorded_gap(reason: str) -> Optional[Tuple[str, str]]:
    """A recorded lane gap reason read back to `(class, criterion)`, or None.

    A class that no longer exists (`taste`) comes back by its old name, so the
    caller can drop it.
    """
    if not reason.startswith(GAP_PREFIX + " "):
        return None
    criterion, sep, text = reason[len(GAP_PREFIX) + 1:].rpartition(", because it needs ")
    if not sep:
        return None
    by_text = {desc: cls for cls, desc in OWNER_CLASSES.items()}
    by_text.update(_LEGACY_CLASS_TEXT)
    cls = by_text.get(text.strip())
    return (cls, criterion.strip()) if cls else None


def gap_reason(cls: str, criterion: str) -> str:
    return f"{GAP_PREFIX} {criterion}, because it needs {OWNER_CLASSES[cls]}"


def explicit_line(body: str) -> Optional[str]:
    """The issue's own `Owner validation: required|none` line, or None."""
    m = _EXPLICIT.search(body or "")
    return m.group(1).lower() if m else None


def ux_proven(pr_body: str) -> bool:
    """True when the PR description carries `/issue-finish`'s fresh visual-proof line."""
    return "VISUAL_PROOF=fresh" in (pr_body or "")


def _first(pattern: re.Pattern, text: str) -> Optional[str]:
    m = pattern.search(text)
    return m.group(1).lower() if m else None


def _live_only_path(files: Sequence[Tuple[str, str]]) -> Optional[str]:
    for _status, path in files:
        norm = path.replace("\\", "/")
        if _NOT_LIVE_INPUT.search(norm):
            continue
        if set(re.split(r"[^a-z0-9]+", norm.lower())) & _LIVE_ONLY_PATH_TOKENS:
            return path
    return None


def _added_surface_path(files: Sequence[Tuple[str, str]]) -> Optional[str]:
    for status, path in files:
        norm = path.replace("\\", "/")
        if status == "added" and not _NOT_A_SURFACE.search(norm) and (
            _PAGE_PATH.search(norm) or _VIEW_PATH.search(norm)
        ):
            return path
    return None


def unproven_surface(labels: Iterable[str], files: Optional[Sequence[Tuple[str, str]]],
                     proven: bool) -> Optional[str]:
    """The added page or view of an enhancement no fresh visual proof covered, or None."""
    if proven or "enhancement" not in {str(label).lower() for label in labels}:
        return None
    return _added_surface_path(list(files or []))


def decide(
    labels: Iterable[str],
    title: str,
    body: str,
    files: Optional[Sequence[Tuple[str, str]]] = None,
    verification: str = VERIFIED_NA,
    gaps: Sequence[Tuple[str, str]] = (),
) -> Decision:
    """Required or none, with every reason that fired.

    `files` are `(status, path)` pairs with GitHub's PR-file statuses
    (`added`, `modified`, `removed`, `renamed`); None means no diff is part of
    this decision (an issue draft, a back-fill). A new page or view never adds an
    owner reason here; `unproven_surface` handles it. `gaps` are `(class, criterion)` pairs from `parse_gap`; `VERIFIED_NO` needs at
    least one, since "could not verify" alone names nothing the owner can do.
    """
    if verification not in VERIFICATION_STATES:
        raise ValueError(f"verification must be one of {VERIFICATION_STATES}, got {verification!r}")
    if (verification == VERIFIED_NO) != bool(gaps):
        raise ValueError("verification 'no' needs at least one owner-only gap, and only 'no' takes gaps")
    enhancement = "enhancement" in {str(label).lower() for label in labels}
    files = list(files or [])
    title = _INLINE_CODE.sub(" ", title or "")
    reasons: List[str] = []

    if explicit_line(body) == REQUIRED:
        reasons.append("the issue says Owner validation: required")
    word = None if verification in _PROVEN else _first(_LIVE_ONLY_TITLE, title)
    if word:
        reasons.append(f"needs a real device or live input ({word}), which a session cannot drive")
    path = _live_only_path(files)
    if path:
        reasons.append(f"changes a live-input path ({path})")
    if enhancement:
        word = _first(_ACCOUNT_TITLE, title)
        if word:
            reasons.append(f"involves a third-party account only the owner holds ({word})")
    reasons.extend(gap_reason(cls, criterion) for cls, criterion in gaps)

    return Decision(REQUIRED if reasons else NONE, tuple(reasons))
