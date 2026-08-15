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


# --- single-select (radio) --------------------------------------------------------


def test_single_reducer_enter_and_space_both_confirm_cursor() -> None:
    from jigga.runtime.term_select import handle_key_single

    state = SelectState(options=_options(3))
    handle_key_single(state, "down")
    handle_key_single(state, "enter")
    assert state.done and not state.cancelled and state.cursor == 1

    state = SelectState(options=_options(3))
    handle_key_single(state, "space")
    assert state.done and state.cursor == 0


def test_single_reducer_cancel_keys() -> None:
    from jigga.runtime.term_select import handle_key_single

    for key in ("q", "esc", "ctrl-c"):
        state = SelectState(options=_options(2))
        handle_key_single(state, key)
        assert state.done and state.cancelled


def test_render_single_radio_markers() -> None:
    from jigga.runtime.term_select import render_single

    state = SelectState(options=_options(2), cursor=1)
    lines = render_single(state, "Pick one", color=False)
    assert lines[1].startswith("│   ○ opt0")
    assert lines[2].startswith("│ ❯ ● opt1")
    assert lines[-1] == "└"
    assert "enter select" in lines[0]


def test_select_one_returns_cursor_index() -> None:
    from jigga.runtime.term_select import select_one

    picked = select_one("Pick", _options(3), out=io.StringIO(),
                        _keys=iter(["down", "down", "enter"]))
    assert picked == 2


def test_select_one_starts_on_default_and_cancel_returns_none() -> None:
    from jigga.runtime.term_select import select_one

    # Enter without moving picks the default
    assert select_one("Pick", _options(3), default_index=1, out=io.StringIO(),
                      _keys=iter(["enter"])) == 1
    # default_index clamped into range
    assert select_one("Pick", _options(2), default_index=9, out=io.StringIO(),
                      _keys=iter(["enter"])) == 1
    assert select_one("Pick", _options(2), out=io.StringIO(), _keys=iter(["q"])) is None


# --- call sites: picker drives every menu on a TTY --------------------------------


def test_channels_setup_uses_picker_for_channel_and_activation(tmp_path, monkeypatch) -> None:
    from jigga.cli import _channels_setup
    from jigga.commands.init import init_runtime
    from jigga.core.io import read_yaml

    paths = init_runtime(tmp_path)
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    titles: list[str] = []
    # sorted catalog is [sms, telegram, webchat] → telegram is index 1;
    # activation pick → index 1 (mention)
    answers = iter([1, 1])
    monkeypatch.setattr("jigga.cli.select_one",
                        lambda title, options, **k: titles.append(title) or next(answers))
    monkeypatch.setattr("jigga.cli.install_capability", lambda *a, **k: 0)
    boom = lambda _p: (_ for _ in ()).throw(AssertionError("typed prompt must not run"))  # noqa: E731

    _channels_setup(paths, prompt=boom, echo=lambda *a, **k: None)

    assert len(titles) == 2
    config = read_yaml(paths.config)
    assert config["channels"]["telegram"]["enabled"] is True
    assert config["channels"]["telegram"]["activation"] == "mention"


def test_channels_setup_picker_cancel_aborts(tmp_path, monkeypatch) -> None:
    from jigga.cli import _channels_setup
    from jigga.commands.init import init_runtime
    from jigga.core.io import read_yaml

    paths = init_runtime(tmp_path)
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.cli.select_one", lambda *a, **k: None)
    monkeypatch.setattr("jigga.cli.install_capability",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not install")))
    _channels_setup(paths, prompt=lambda _p: "", echo=lambda *a, **k: None)
    assert "channels" not in (read_yaml(paths.config) or {})


def test_model_setup_uses_picker_for_provider(tmp_path, monkeypatch) -> None:
    from jigga.cli import _model_setup
    from jigga.commands.init import init_runtime
    from jigga.core.io import read_yaml

    paths = init_runtime(tmp_path)
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.cli.select_one", lambda title, options, **k: 1)  # Dry-run
    boom = lambda _p: (_ for _ in ()).throw(AssertionError("typed prompt must not run"))  # noqa: E731

    _model_setup(paths, prompt=boom, echo=lambda *a, **k: None)
    config = read_yaml(paths.config)
    assert config["models"]["defaults"]["provider"] == "dry_run"


def test_model_setup_picker_cancel_changes_nothing(tmp_path, monkeypatch) -> None:
    from jigga.cli import _model_setup
    from jigga.commands.init import init_runtime
    from jigga.core.io import read_yaml

    paths = init_runtime(tmp_path)
    before = read_yaml(paths.config)["models"]["defaults"]["provider"]
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.cli.select_one", lambda *a, **k: None)
    _model_setup(paths, prompt=lambda _p: "", echo=lambda *a, **k: None)
    assert read_yaml(paths.config)["models"]["defaults"]["provider"] == before


def test_capability_install_menu_uses_picker(monkeypatch) -> None:
    from jigga.commands.install import _prompt_for_capability
    from jigga.optional_capabilities import list_available

    available = list_available()
    assert available
    monkeypatch.setattr("jigga.commands.install.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.commands.install.select_one", lambda title, options, **k: 0)
    boom = lambda _p: (_ for _ in ()).throw(AssertionError("typed prompt must not run"))  # noqa: E731
    cap = _prompt_for_capability(available, input_fn=boom, print_fn=lambda *a, **k: None)
    assert cap is available[0]

    monkeypatch.setattr("jigga.commands.install.select_one", lambda *a, **k: None)
    assert _prompt_for_capability(available, input_fn=boom, print_fn=lambda *a, **k: None) is None


def test_onboard_choose_uses_picker_and_cancel_falls_to_default(monkeypatch) -> None:
    from jigga.commands.onboard import _choose

    options = [("chief", "Chief of staff"), ("assistant", "Personal assistant")]
    monkeypatch.setattr("jigga.commands.onboard.supports_picker", lambda *a, **k: True)
    seen: dict = {}

    def fake_select(title, opts, *, default_index=0, **k):
        seen["default_index"] = default_index
        return 1

    monkeypatch.setattr("jigga.commands.onboard.select_one", fake_select)
    boom = lambda _p: (_ for _ in ()).throw(AssertionError("typed prompt must not run"))  # noqa: E731
    assert _choose(boom, lambda *a, **k: None, "Role?", options, default="assistant") == "assistant"
    assert seen["default_index"] == 1                      # cursor starts on the default

    monkeypatch.setattr("jigga.commands.onboard.select_one", lambda *a, **k: None)
    assert _choose(boom, lambda *a, **k: None, "Role?", options, default="chief") == "chief"


def test_channels_setup_activation_cancel_defaults_to_always(tmp_path, monkeypatch) -> None:
    from jigga.cli import _channels_setup
    from jigga.commands.init import init_runtime
    from jigga.core.io import read_yaml

    paths = init_runtime(tmp_path)
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    answers = iter([1, None])                      # pick telegram; cancel the activation pick
    monkeypatch.setattr("jigga.cli.select_one", lambda *a, **k: next(answers))
    monkeypatch.setattr("jigga.cli.install_capability", lambda *a, **k: 0)
    _channels_setup(paths, prompt=lambda _p: "", echo=lambda *a, **k: None)
    assert read_yaml(paths.config)["channels"]["telegram"]["activation"] == "always"
