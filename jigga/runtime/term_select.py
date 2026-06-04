"""Arrow-key multi-select for interactive CLI flows (stdlib-only).

A clack-style checkbox picker (the OpenClaw onboarding look): ↑/↓ (or j/k)
move, space toggles, `a` toggles all, Enter confirms, `q`/Esc/Ctrl-C cancels.

    ◆ Install example recipes   ↑/↓ move · space toggle · a all · enter confirm
    │ ❯ ◼ personal_admin_team    team   Help the user manage daily schedule…
    │   ◻ marketing_team         team   A marketing team that turns a brief…
    └ 1 selected

The pure-logic core (key decoding, state reducer, renderer) is separate from
the raw-terminal driver so tests drive it without a TTY. Callers must fall
back to a typed prompt when `supports_picker()` is false (pipes, dumb
terminals, tests) — `multi_select` refuses to guess on a non-TTY.
"""

from __future__ import annotations

import os
import select
import sys
from dataclasses import dataclass
from typing import TextIO

_HINT = "↑/↓ move · space toggle · a all · enter confirm · q skip"
_HINT_SINGLE = "↑/↓ move · enter select · q cancel"


@dataclass
class Option:
    label: str
    detail: str = ""
    selected: bool = False


@dataclass
class SelectState:
    options: list[Option]
    cursor: int = 0
    done: bool = False
    cancelled: bool = False


def handle_key(state: SelectState, key: str) -> SelectState:
    """Pure reducer: one decoded key → next state. Unknown keys are no-ops."""
    count = len(state.options)
    if key in {"up", "k"}:
        state.cursor = (state.cursor - 1) % count
    elif key in {"down", "j"}:
        state.cursor = (state.cursor + 1) % count
    elif key == "space":
        option = state.options[state.cursor]
        option.selected = not option.selected
    elif key == "a":
        target = not all(o.selected for o in state.options)
        for option in state.options:
            option.selected = target
    elif key == "enter":
        state.done = True
    elif key in {"q", "esc", "ctrl-c"}:
        state.done = True
        state.cancelled = True
    return state


def handle_key_single(state: SelectState, key: str) -> SelectState:
    """Single-select reducer: Enter (or space) chooses the cursor item."""
    count = len(state.options)
    if key in {"up", "k"}:
        state.cursor = (state.cursor - 1) % count
    elif key in {"down", "j"}:
        state.cursor = (state.cursor + 1) % count
    elif key in {"enter", "space"}:
        state.done = True
    elif key in {"q", "esc", "ctrl-c"}:
        state.done = True
        state.cancelled = True
    return state


def _paint(text: str, code: str, color: bool) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if color else text


def render(state: SelectState, title: str, *, color: bool = True) -> list[str]:
    """The checkbox (multi-select) frame as a list of lines."""
    lines = [f"{_paint('◆', '36', color)} {title}   {_paint(_HINT, '2', color)}"]
    for index, option in enumerate(state.options):
        cursor = "❯" if index == state.cursor else " "
        box = _paint("◼", "32", color) if option.selected else "◻"
        label = option.label
        if index == state.cursor:
            cursor = _paint("❯", "36", color)
            label = _paint(label, "1", color)
        detail = f"   {_paint(option.detail, '2', color)}" if option.detail else ""
        lines.append(f"│ {cursor} {box} {label}{detail}")
    chosen = sum(1 for o in state.options if o.selected)
    lines.append(f"└ {chosen} selected")
    return lines


def render_single(state: SelectState, title: str, *, color: bool = True) -> list[str]:
    """The radio (single-select) frame: ● marks the cursor row — what Enter
    will choose — ○ the rest."""
    lines = [f"{_paint('◆', '36', color)} {title}   {_paint(_HINT_SINGLE, '2', color)}"]
    for index, option in enumerate(state.options):
        on_cursor = index == state.cursor
        cursor = _paint("❯", "36", color) if on_cursor else " "
        radio = _paint("●", "32", color) if on_cursor else "○"
        label = _paint(option.label, "1", color) if on_cursor else option.label
        detail = f"   {_paint(option.detail, '2', color)}" if option.detail else ""
        lines.append(f"│ {cursor} {radio} {label}{detail}")
    lines.append("└")
    return lines


def decode_key(seq: bytes) -> str:
    """Raw byte sequence → semantic key name ('other' when unrecognized)."""
    if seq in {b"\x1b[A", b"\x1bOA"}:
        return "up"
    if seq in {b"\x1b[B", b"\x1bOB"}:
        return "down"
    if seq == b"\x1b":
        return "esc"
    if seq in {b"\r", b"\n"}:
        return "enter"
    if seq == b" ":
        return "space"
    if seq == b"\x03":
        return "ctrl-c"
    try:
        char = seq.decode("utf-8").lower()
    except UnicodeDecodeError:
        return "other"
    if char in {"a", "j", "k", "q"}:
        return char
    return "other"


def _read_key(stdin_fd: int) -> str:
    """Blocking single-keypress read. An ESC byte is disambiguated from an
    arrow-key sequence with a short select() — terminals send the full
    sequence in one burst, a human Esc press arrives alone."""
    first = os.read(stdin_fd, 1)
    if first != b"\x1b":
        return decode_key(first)
    sequence = first
    while select.select([stdin_fd], [], [], 0.03)[0]:
        sequence += os.read(stdin_fd, 1)
        if len(sequence) >= 3:
            break
    return decode_key(sequence)


def supports_picker(stdin: TextIO | None = None, stdout: TextIO | None = None) -> bool:
    """True when an interactive raw-mode picker can run here."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        import termios  # noqa: F401 — POSIX only; absent on Windows
        import tty  # noqa: F401
    except ImportError:
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    try:
        return stdin.isatty() and stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _run_loop(state: SelectState, title: str, *, keys, out: TextIO, color: bool,
              reducer=handle_key, renderer=render) -> None:
    """Render/redraw loop, driven by an iterator of decoded keys. Factored out
    so tests can run the full loop without a terminal."""
    frame = renderer(state, title, color=color)
    out.write("\n".join(frame) + "\n")
    out.flush()
    for key in keys:
        reducer(state, key)
        out.write(f"\x1b[{len(frame)}A")  # cursor to the top of the frame
        frame = renderer(state, title, color=color)
        out.write("\x1b[0J")  # clear to end of screen, then repaint
        out.write("\n".join(frame) + "\n")
        out.flush()
        if state.done:
            return
    state.done = True  # key stream exhausted (tests): confirm as-is


def _run(state: SelectState, title: str, *, out: TextIO | None, _keys, reducer, renderer) -> None:
    """Drive the loop from injected keys (tests) or the raw terminal."""
    out = out if out is not None else sys.stdout
    if _keys is not None:  # test path: no terminal needed
        _run_loop(state, title, keys=_keys, out=out, color=False,
                  reducer=reducer, renderer=renderer)
        return

    import termios
    import tty

    stdin_fd = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin_fd)
    out.write("\x1b[?25l")  # hide cursor
    try:
        tty.setcbreak(stdin_fd)

        def _key_stream():
            while True:
                try:
                    yield _read_key(stdin_fd)
                except KeyboardInterrupt:  # cbreak leaves SIGINT enabled
                    yield "ctrl-c"

        _run_loop(state, title, keys=_key_stream(), out=out,
                  color=os.environ.get("NO_COLOR") is None,
                  reducer=reducer, renderer=renderer)
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        out.write("\x1b[?25h")  # show cursor
        out.flush()


def multi_select(title: str, options: list[Option], *, out: TextIO | None = None,
                 _keys=None) -> list[int] | None:
    """Checkbox picker; returns selected indices, or None when cancelled.
    Caller is responsible for checking `supports_picker()` first (except in
    tests, which inject `_keys`)."""
    state = SelectState(options=options)
    _run(state, title, out=out, _keys=_keys, reducer=handle_key, renderer=render)
    return None if state.cancelled else [i for i, o in enumerate(options) if o.selected]


def select_one(title: str, options: list[Option], *, default_index: int = 0,
               out: TextIO | None = None, _keys=None) -> int | None:
    """Radio picker; returns the chosen index, or None when cancelled. The
    cursor starts on `default_index` so Enter-without-moving picks the default.
    Same `supports_picker()` contract as `multi_select`."""
    state = SelectState(options=options, cursor=max(0, min(default_index, len(options) - 1)))
    _run(state, title, out=out, _keys=_keys, reducer=handle_key_single, renderer=render_single)
    return None if state.cancelled else state.cursor
