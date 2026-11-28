"""Team Mirror: publish a personal repo's main to a team repo as one snapshot commit.

The personal repo stays the source of truth. A publish takes its main's file
tree, drops the link's leave-out paths, scans what is left for personal data,
and commits exactly that tree on top of the team repo's main, authored by this
computer's git identity, so no personal commit or name reaches the team repo.
Nothing flows back. Links live in the machine-local webapp config, the last
publish in the runtime data root; neither repo carries any bookkeeping.

``links``    the per-project link rows and the work identity
``snapshot`` the git plumbing in Lite's own scratch repo
``scan``     the personal-data scan, the vendored ratchet's rules and term list
``forge``    opening and merging the PR, chosen from the team URL's host
``state``    the last published commit per project
``publish``  status, preview and publish, which put the pieces together
"""
