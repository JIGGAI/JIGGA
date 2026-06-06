"""`jigga plugins` — out-of-process supervised sidecar apps (jiggaview is the
reference). Install: fetch → manifest → scan → approve → setup → service."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.plugins import install_plugin, list_plugins, plugin_dir, uninstall_plugin


@pytest.fixture(autouse=True)
def _no_real_system(monkeypatch):
    """Tests never touch launchd/systemd or run real subprocesses: the service
    layer records instead of executing, and setup argvs run through a fake
    runner. Tests asserting on these override per-case."""
    services: dict = {"installed": [], "uninstalled": []}
    monkeypatch.setattr(
        "jigga.runtime.plugins.install_app_service",
        lambda name, argv, **kw: services["installed"].append({"name": name, "argv": argv, **kw})
        or {"backend": "systemd", "started": True, "unit_path": "/x"},
    )
    monkeypatch.setattr(
        "jigga.runtime.plugins.uninstall_app_service",
        lambda name, **kw: services["uninstalled"].append(name) or {"backend": "systemd", "removed": True},
    )
    monkeypatch.setattr(
        "jigga.runtime.plugins.status_app_service",
        lambda name, **kw: {"backend": "systemd", "installed": True, "running": True},
    )
    # CLI imports these from jigga.runtime.service directly — stub those too.
    monkeypatch.setattr(
        "jigga.runtime.service.install_app_service",
        lambda name, argv, **kw: {"backend": "systemd", "started": True},
    )
    monkeypatch.setattr(
        "jigga.runtime.service.uninstall_app_service",
        lambda name, **kw: {"backend": "systemd", "removed": True},
    )
    monkeypatch.setattr(
        "jigga.runtime.service.status_app_service",
        lambda name, **kw: {"backend": "systemd", "installed": True, "running": True},
    )
    return services


@pytest.fixture()
def _setup_runner():
    calls: list = []

    def runner(argv, cwd, env):
        calls.append({"argv": argv, "cwd": str(cwd), "env": env})
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _make_plugin_source(tmp_path: Path, *, name="jiggaview", port=4400) -> Path:
    source = tmp_path / "plugin-src"
    source.mkdir()
    write_yaml(source / "manifest.yaml", {
        "name": name, "version": "0.1.0",
        "summary": "Web dashboard plugin for JIGGA.",
        "type": "app",
        "run": ["node", "server.js"],
        "setup": [["npm", "ci"], ["npm", "run", "build"]],
        "port": port,
        "risk_level": "medium",
    })
    (source / "server.js").write_text("// app\n", encoding="utf-8")
    return source


def test_install_runs_setup_approves_and_registers_service(tmp_path: Path, _no_real_system, _setup_runner) -> None:
    paths = init_runtime(tmp_path / "home")
    source = _make_plugin_source(tmp_path)

    summary = install_plugin(paths, str(source), run_fn=_setup_runner,
                             service_install_fn=lambda name, argv, **kw:
                             _no_real_system["installed"].append({"name": name, "argv": argv, **kw})
                             or {"backend": "systemd", "started": True})

    assert summary["name"] == "jiggaview" and summary["port"] == 4400
    target = plugin_dir(paths.home, "jiggaview")
    assert (target / "manifest.yaml").exists() and (target / "server.js").exists()
    # setup argvs ran IN the plugin dir with JIGGA_HOME + PORT in env
    assert [c["argv"] for c in _setup_runner.calls] == [["npm", "ci"], ["npm", "run", "build"]]
    assert all(c["cwd"] == str(target) for c in _setup_runner.calls)
    assert _setup_runner.calls[0]["env"]["PORT"] == "4400"
    assert _setup_runner.calls[0]["env"]["JIGGA_HOME"] == str(paths.home)
    # service registered with the manifest's run argv, cwd = plugin dir
    assert _no_real_system["installed"][-1]["argv"] == ["node", "server.js"]
    # approval recorded (capability trust gate)
    from jigga.runtime.capabilities import approvals_path
    approvals = json.loads(approvals_path(paths.policies).read_text(encoding="utf-8"))
    assert "jiggaview" in (approvals.get("approvals") or {})
    # audited
    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "plugin.installed" for e in events)


def test_failed_setup_rolls_back_cleanly(tmp_path: Path, _no_real_system) -> None:
    paths = init_runtime(tmp_path / "home")
    source = _make_plugin_source(tmp_path)

    def failing_runner(argv, cwd, env):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="npm exploded")

    with pytest.raises(ValueError, match="npm exploded"):
        install_plugin(paths, str(source), run_fn=failing_runner)
    assert not plugin_dir(paths.home, "jiggaview").exists()        # clean retry possible


def test_double_install_refused(tmp_path: Path, _no_real_system, _setup_runner) -> None:
    paths = init_runtime(tmp_path / "home")
    source = _make_plugin_source(tmp_path)
    install_plugin(paths, str(source), run_fn=_setup_runner)
    with pytest.raises(ValueError, match="already installed"):
        install_plugin(paths, str(source), run_fn=_setup_runner)


def test_non_app_manifest_rejected(tmp_path: Path, _no_real_system) -> None:
    paths = init_runtime(tmp_path / "home")
    source = tmp_path / "notapp"
    source.mkdir()
    write_yaml(source / "manifest.yaml", {
        "name": "x", "version": "1", "summary": "s", "type": "native",
        "actions": ["x.y"],
    })
    with pytest.raises(ValueError, match="not an app plugin"):
        install_plugin(paths, str(source))
    assert not plugin_dir(paths.home, "x").exists()


def test_list_and_uninstall(tmp_path: Path, _no_real_system, _setup_runner) -> None:
    paths = init_runtime(tmp_path / "home")
    install_plugin(paths, str(_make_plugin_source(tmp_path)), run_fn=_setup_runner)

    plugins = list_plugins(paths)
    assert len(plugins) == 1
    assert plugins[0]["name"] == "jiggaview" and plugins[0]["running"] is True

    result = uninstall_plugin(paths, "jiggaview")
    assert result["removed"] is True
    assert _no_real_system["uninstalled"] == ["jiggaview"]
    assert not plugin_dir(paths.home, "jiggaview").exists()
    assert list_plugins(paths) == []
    with pytest.raises(ValueError, match="not installed"):
        uninstall_plugin(paths, "jiggaview")


def test_cli_install_list_uninstall(tmp_path: Path, monkeypatch, _no_real_system, _setup_runner, capsys) -> None:
    monkeypatch.setattr("jigga.runtime.plugins._default_setup_runner", _setup_runner)
    home = tmp_path / "home"
    init_runtime(home)
    source = _make_plugin_source(tmp_path)

    assert main(["--home", str(home), "plugins", "install", str(source), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["name"] == "jiggaview"

    assert main(["--home", str(home), "plugins", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed and listed[0]["name"] == "jiggaview"

    assert main(["--home", str(home), "plugins", "status", "jiggaview"]) == 0
    assert "running" in capsys.readouterr().out

    assert main(["--home", str(home), "plugins", "uninstall", "jiggaview"]) == 0
    capsys.readouterr()
    assert main(["--home", str(home), "plugins", "list"]) == 0
    assert "No plugins installed" in capsys.readouterr().out


def test_app_manifest_validation() -> None:
    from jigga.runtime.capabilities import CapabilityManifest

    with pytest.raises(ValueError, match="non-empty 'run'"):
        CapabilityManifest.from_dict({"name": "a", "version": "1", "summary": "s", "type": "app"})
    with pytest.raises(ValueError, match="declare no actions"):
        CapabilityManifest.from_dict({"name": "a", "version": "1", "summary": "s", "type": "app",
                                      "run": ["x"], "actions": ["a.b"]})
    ok = CapabilityManifest.from_dict({"name": "a", "version": "1", "summary": "s", "type": "app",
                                       "run": ["node", "s.js"], "port": 4400,
                                       "setup": [["npm", "ci"]], "app_env": {"FOO": "1"}})
    assert ok.run == ["node", "s.js"] and ok.port == 4400 and ok.app_env == {"FOO": "1"}
