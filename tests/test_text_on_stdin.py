"""A message body belongs on stdin, not in argv.

argv is world-readable through /proc: while `jigga webchat send --text "..."`
runs, every other account on the machine can read that message out of `ps`. The
same goes for a durable memory entry. Both commands still accept `--text` (a
human typing one at a shell is not the leak worth optimising against), but a
caller holding the text — jiggaview, a script — can now pipe it.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from jigga.cli import _text_or_stdin, main
from jigga.commands.init import init_runtime


def _team(home: Path) -> None:
    from jigga.core.io import write_yaml

    write_yaml(home / "teams" / "eng.yaml", {"id": "eng", "name": "Eng", "members": []})
    main(["--home", str(home), "team", "init", "eng"])



# --- the helper --------------------------------------------------------------


def test_an_explicit_value_wins_and_stdin_is_not_read(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("should not be read"))
    assert _text_or_stdin("given", "--text") == "given"


def test_it_reads_stdin_when_the_flag_is_omitted(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("piped body\n"))
    assert _text_or_stdin(None, "--text") == "piped body\n"


def test_an_interactive_terminal_is_an_error_not_a_hang(monkeypatch) -> None:
    # Blocking on a tty reads to a person as a crash, and the fix ("pass --text
    # or pipe it") is the only useful thing to say.
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Tty(""))
    with pytest.raises(ValueError, match="pipe the text in on stdin"):
        _text_or_stdin(None, "--text")


def test_an_empty_pipe_is_content_not_a_missing_argument(monkeypatch) -> None:
    # Distinct from the tty case: a caller that piped nothing said something,
    # and each command decides for itself whether empty is acceptable.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert _text_or_stdin(None, "--text") == ""


# --- team memory add ---------------------------------------------------------


def test_memory_add_takes_the_entry_on_stdin(tmp_path: Path, capsys, monkeypatch) -> None:
    init_runtime(tmp_path)
    _team(tmp_path)
    capsys.readouterr()  # drop the team-init chatter; only the command's own output matters
    monkeypatch.setattr("sys.stdin", io.StringIO("we ship on Fridays\n"))
    assert main(["--home", str(tmp_path), "team", "memory", "add", "eng", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "we ship on Fridays"


def test_memory_add_still_takes_the_flag(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _team(tmp_path)
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "team", "memory", "add", "eng",
                 "--text", "typed by hand", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "typed by hand"


def test_memory_add_refuses_an_empty_body_either_way(tmp_path: Path, capsys, monkeypatch) -> None:
    init_runtime(tmp_path)
    _team(tmp_path)
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
    assert main(["--home", str(tmp_path), "team", "memory", "add", "eng"]) == 1
    assert "empty" in capsys.readouterr().out


# --- webchat send ------------------------------------------------------------


def test_webchat_send_takes_the_message_on_stdin(tmp_path: Path, capsys, monkeypatch) -> None:
    init_runtime(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("hello from a pipe"))
    assert main(["--home", str(tmp_path), "webchat", "send", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["message"]["text"] == "hello from a pipe"


def test_webchat_send_still_takes_the_flag(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "webchat", "send", "--json", "--text", "typed"]) == 0
    assert json.loads(capsys.readouterr().out)["message"]["text"] == "typed"


def test_webchat_send_on_a_terminal_with_no_text_says_what_to_do(tmp_path: Path, capsys,
                                                                 monkeypatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    init_runtime(tmp_path)
    monkeypatch.setattr("sys.stdin", Tty(""))
    assert main(["--home", str(tmp_path), "webchat", "send", "--json"]) == 1
    assert "pipe the text in on stdin" in capsys.readouterr().err
