"""The fleet chief's deterministic operations, behind the `chief_ops.py` CLI.

`launcher` is the only module that talks to the app-launcher webapp or `gh`;
`rails`, `digest`, `mailbox` and `compact` decide and format without I/O beyond
the mailbox directory; `commands` wires one handler per subcommand and `cli`
builds the argv. `chief_ops.py` stays the entry point and re-exports the names
its callers import.
"""
