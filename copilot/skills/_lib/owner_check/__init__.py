"""Owner checks: work that needs Roberto's validation, or a step of his before it starts.

The contract is `docs/owner-check.md`. `rule` decides required/none from what an
issue says and what its PR changed, `block` owns the comment-block format, `github`
is the only module that talks to `gh`, and `cli` is the command line every skill
calls. stdlib only, so the package ports to the lite fleet unchanged.
"""

from owner_check.github import list_items

__all__ = ["list_items"]
