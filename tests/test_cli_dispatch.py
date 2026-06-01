from __future__ import annotations

import argparse

from jigga.cli import _COMMANDS, build_parser


def _subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def test_every_command_has_a_handler() -> None:
    """Every top-level CLI command must map to a handler in the dispatch table.
    Guards against a command being parseable but unreachable (the H0 class of
    bug, where a handler was orphaned after a return)."""
    commands = _subcommand_names(build_parser())
    assert commands, "no subcommands found on the parser"
    missing = commands - set(_COMMANDS)
    assert not missing, f"commands with no handler: {sorted(missing)}"


def test_no_orphan_handlers() -> None:
    """Every handler in the dispatch table corresponds to a real command."""
    commands = _subcommand_names(build_parser())
    orphans = set(_COMMANDS) - commands
    assert not orphans, f"handlers with no command: {sorted(orphans)}"
