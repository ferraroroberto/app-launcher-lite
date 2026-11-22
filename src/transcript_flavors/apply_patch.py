"""The ``apply_patch`` patch text: ``*** Begin Patch`` .. ``*** End Patch``.

Copilot's ``apply_patch`` tool call carries its edit in this format; the
sections are folded into the same ``edited`` / ``wrote`` / ``deleted``
steps a Chat step and the Changed files panel show.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.transcript_flavors._shared import Hunk, _added_hunks, _with_diff, diff_counts

# The patch text names each file in an ``*** Add File:`` / ``*** Update File:``
# / ``*** Delete File:`` section. It carries no line numbers, so an update's
# diff is unnumbered; an Add File starts at line 1.
_PATCH_BEGIN = "*** Begin Patch"
_PATCH_END = "*** End Patch"
_PATCH_FAILED = "apply_patch verification failed"
_ADD, _UPDATE, _DELETE, _MOVE = "*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "


def parse_apply_patch(text: str) -> List[Dict[str, Any]]:
    """The file sections of one patch, in order, each as an action dict
    (``wrote`` for an Add, ``edited`` for an Update, ``deleted`` for a
    Delete; an Update that moves the file edits the new path and deletes the
    old). A text that is not a complete Begin..End patch reads as nothing."""
    lines = text.splitlines()
    begin = next((i for i, line in enumerate(lines) if line.strip() == _PATCH_BEGIN), None)
    if begin is None or not any(line.strip() == _PATCH_END for line in lines[begin:]):
        return []
    sections: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    hunk: Optional[Hunk] = None
    for line in lines[begin + 1:]:
        if line.strip() == _PATCH_END:
            break
        header = next((h for h in (_ADD, _UPDATE, _DELETE) if line.startswith(h)), None)
        if header:
            cur = {"op": header, "path": line[len(header):].strip(), "lines": [], "hunks": [], "move": None}
            sections.append(cur)
            hunk = None
        elif cur is None or line.startswith("*** End of File"):
            continue
        elif line.startswith(_MOVE) and cur["op"] == _UPDATE:
            cur["move"] = line[len(_MOVE):].strip()
        elif cur["op"] == _ADD:
            if line.startswith("+"):
                cur["lines"].append(line[1:])
        elif cur["op"] == _UPDATE:
            if line.startswith("@@"):
                hunk = {"old_start": None, "new_start": None, "lines": []}
                cur["hunks"].append(hunk)
            elif not line or line[0] in "+- ":
                if hunk is None:
                    hunk = {"old_start": None, "new_start": None, "lines": []}
                    cur["hunks"].append(hunk)
                hunk["lines"].append(line or " ")
    actions: List[Dict[str, Any]] = []
    for sec in sections:
        path = sec["path"]
        if not path:
            continue
        if sec["op"] == _DELETE:
            actions.append({"verb": "deleted", "path": path})
        elif sec["op"] == _ADD:
            hunks = _added_hunks("\n".join(sec["lines"]), numbered=True)
            actions.append(_with_diff(
                {"verb": "wrote", "path": path, "added": len(sec["lines"]), "removed": 0, "created": True},
                hunks,
            ))
        else:
            hunks = [h for h in sec["hunks"] if h["lines"]]
            added, removed = diff_counts(hunks)
            target = sec["move"] or path
            actions.append(_with_diff({"verb": "edited", "path": target, "added": added, "removed": removed}, hunks))
            if sec["move"] and sec["move"] != path:
                actions.append({"verb": "deleted", "path": path})
    return actions

