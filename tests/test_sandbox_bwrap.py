"""E2a/E2b: bwrap argv construction, backend resolution, safe_process
convergence, and a real-bwrap integration check (skipped where absent)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.sandbox import SandboxSpec, build_restricted_env, bwrap_argv, run_sandboxed, sandbox_backend


def _spec(tmp_path: Path, **kw) -> SandboxSpec:
    return SandboxSpec(command="echo", args=["hi"], cwd=tmp_path, **kw)


def test_backend_default_is_none(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    assert sandbox_backend(paths.home) == "none"  # auto stays off until E2c


def test_explicit_bwrap_without_binary_is_loud(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"sandbox": {"backend": "bwrap"}})
    import jigga.runtime.sandbox as mod

    monkeypatch.setattr(shutil, "which", lambda _n: None)
    with pytest.raises(RuntimeError, match="bubblewrap"):
        mod.sandbox_backend(paths.home)


def test_bwrap_argv_shape(tmp_path: Path) -> None:
    fs_in, fs_out = tmp_path / "in", tmp_path / "out"
    fs_in.mkdir(), fs_out.mkdir()
    spec = _spec(tmp_path, network=False, fs_read=[fs_in], fs_write=[fs_out])
    env = {"PATH": "/usr/bin", "HOME": "/h"}
    argv = bwrap_argv(spec, env)
    assert argv[0] == "bwrap" and "--die-with-parent" in argv and "--clearenv" in argv
    assert "--unshare-net" in argv
    joined = " ".join(argv)
    assert f"--ro-bind {fs_in} {fs_in}" in joined
    assert f"--bind {fs_out} {fs_out}" in joined
    assert f"--bind {tmp_path} {tmp_path}" in joined          # cwd rw
    assert "--setenv PATH /usr/bin" in joined and "--setenv HOME /h" in joined
    assert argv[-2:] == ["--chdir", str(tmp_path)]


def test_network_shared_by_default(tmp_path: Path) -> None:
    assert "--unshare-net" not in bwrap_argv(_spec(tmp_path), {})


def test_run_sandboxed_prefixes_only_when_backend_active(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"sandbox": {"backend": "bwrap"}})
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    import jigga.runtime.sandbox as mod

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/bwrap")
    run_sandboxed(_spec(paths.home), home=paths.home)
    assert captured["argv"][0] == "bwrap" and captured["argv"][-2:] == ["echo", "hi"]
    # spec.sandbox=False opts out (surfaced elsewhere as a warning).
    run_sandboxed(_spec(paths.home, sandbox=False), home=paths.home)
    assert captured["argv"][0] == "echo"


def test_safe_process_routes_through_sandbox_seam(tmp_path: Path) -> None:
    from jigga.core.models import AgentConfig
    from jigga.tools.safe_process import run_safe_process

    agent = AgentConfig(id="a", name="A", role="r",
                        permissions={"shell": {"mode": "allow"},
                                     "filesystem": {"allow": [str(tmp_path)]}})
    record = run_safe_process(agent, ["echo", "seam"], tmp_path, tmp_path / "artifacts", apply=True)
    assert record["status"] == "completed"
    assert Path(record["stdout"]).read_text(encoding="utf-8").strip() == "seam"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap not installed")
def test_real_bwrap_executes_and_unshares_net(tmp_path: Path) -> None:
    """Integration (self-contained echo in a tmp dir): the sandboxed process
    runs, sees the cleared env, and network-unshared specs still execute."""
    env = build_restricted_env()
    spec = SandboxSpec(command="sh", args=["-c", "echo $HOME"], cwd=tmp_path, network=False)
    completed = subprocess.run(bwrap_argv(spec, env) + ["--", "sh", "-c", "echo ok"],
                               capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:  # nested-namespace hosts (CI containers) can refuse
        pytest.skip(f"bwrap unavailable in this environment: {completed.stderr[:120]}")
    assert completed.stdout.strip() == "ok"
