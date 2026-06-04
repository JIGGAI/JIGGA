"""The arrow-key picker's pure core: key decoding, state reducer, renderer,
and the render loop driven by an injected key stream (no TTY needed)."""

from __future__ import annotations

import io

from jigga.runtime.term_select import (
    Option,
    SelectState,
    decode_key,
    handle_key,
    multi_select,
    render,
    supports_picker,
)


def _options(n: int = 3) -> list[Option]:
    return [Option(label=f"opt{i}", detail=f"detail {i}") for i in range(n)]


# --- key decoding -------------------------------------------------------------


def test_decode_key_covers_arrows_controls_and_letters() -> None:
    assert decode_key(b"\x1b[A") == "up"
    assert decode_key(b"\x1bOB") == "down"          # application-cursor mode variant
    assert decode_key(b"\x1b") == "esc"
    assert decode_key(b"\r") == "enter"
    assert decode_key(b"\n") == "enter"
    assert decode_key(b" ") == "space"
    assert decode_key(b"\x03") == "ctrl-c"
    assert decode_key(b"K") == "k"                   # case-insensitive vim keys
    assert decode_key(b"x") == "other"
    assert decode_key(b"\xff") == "other"            # undecodable byte is a no-op


# --- reducer -------------------------------------------------------------------


def test_cursor_moves_and_wraps() -> None:
    state = SelectState(options=_options(3))
    handle_key(state, "down")
    assert state.cursor == 1
    handle_key(state, "j")
    handle_key(state, "down")
    assert state.cursor == 0                          # wrapped past the end
    handle_key(state, "up")
    assert state.cursor == 2                          # wrapped backwards
    handle_key(state, "k")
    assert state.cursor == 1


def test_space_toggles_and_a_toggles_all() -> None:
    state = SelectState(options=_options(3))
    handle_key(state, "space")
    assert [o.selected for o in state.options] == [True, False, False]
    handle_key(state, "space")
    assert not state.options[0].selected
    handle_key(state, "a")
    assert all(o.selected for o in state.options)
    handle_key(state, "a")                            # all-on → all-off
    assert not any(o.selected for o in state.options)


def test_enter_confirms_and_q_esc_cancel() -> None:
    state = SelectState(options=_options())
    handle_key(state, "enter")
    assert state.done and not state.cancelled
    for key in ("q", "esc", "ctrl-c"):
        state = SelectState(options=_options())
        handle_key(state, key)
        assert state.done and state.cancelled


def test_unknown_key_is_a_noop() -> None:
    state = SelectState(options=_options())
    handle_key(state, "other")
    assert state.cursor == 0 and not state.done


# --- renderer -------------------------------------------------------------------


def test_render_marks_cursor_and_selection() -> None:
    state = SelectState(options=_options(2))
    state.options[1].selected = True
    state.cursor = 1
    lines = render(state, "Pick", color=False)
    assert "◆ Pick" in lines[0]
    assert lines[1].startswith("│   ◻ opt0")          # not cursor, not selected
    assert lines[2].startswith("│ ❯ ◼ opt1")          # cursor + selected
    assert lines[-1] == "└ 1 selected"
    assert "detail 0" in lines[1]


def test_render_color_wraps_ansi_only_when_enabled() -> None:
    state = SelectState(options=_options(1))
    assert not any("\x1b[" in line for line in render(state, "t", color=False))
    assert any("\x1b[" in line for line in render(state, "t", color=True))


# --- loop (injected keys, no TTY) ------------------------------------------------


def test_multi_select_full_flow_with_injected_keys() -> None:
    out = io.StringIO()
    # select first, move down twice, select third, confirm
    picked = multi_select("Pick", _options(3), out=out,
                          _keys=iter(["space", "down", "down", "space", "enter"]))
    assert picked == [0, 2]
    assert "◆ Pick" in out.getvalue()


def test_multi_select_cancel_returns_none() -> None:
    picked = multi_select("Pick", _options(2), out=io.StringIO(),
                          _keys=iter(["space", "q"]))
    assert picked is None


def test_multi_select_confirm_nothing_returns_empty() -> None:
    picked = multi_select("Pick", _options(2), out=io.StringIO(), _keys=iter(["enter"]))
    assert picked == []


# --- environment gate -------------------------------------------------------------


def test_supports_picker_false_for_non_tty_and_dumb_term(monkeypatch) -> None:
    class NoTty(io.StringIO):
        def isatty(self) -> bool:
            return False

    assert supports_picker(stdin=NoTty(), stdout=NoTty()) is False

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("TERM", "dumb")
    assert supports_picker(stdin=Tty(), stdout=Tty()) is False
    monkeypatch.setenv("TERM", "xterm-256color")
    assert supports_picker(stdin=Tty(), stdout=Tty()) is True
