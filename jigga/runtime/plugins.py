"""JIGGA plugins — out-of-process supervised sidecar apps (jiggaview is the
reference implementation).

A plugin is a directory with a `manifest.yaml` of `type: app`: a long-running
`run` argv, optional one-shot `setup` argvs (npm ci, build …), an optional
`port`, and the usual capability trust surface (summary, risk_level,
permissions). Unlike capabilities, apps declare no actions — they are never
dispatched by agents; JIGGA installs, approves, and *supervises* them:

    jigga plugins install <local dir | git url>
    jigga plugins list | status | start | stop | uninstall <name>

Install flow: fetch → load+validate manifest → security scan → record approval
(same registry gate as capabilities; the manifest hash pins what was approved)
→ run `setup` argvs in the plugin dir → register a per-plugin user service
(`ai.jigga.plugin.<name>` / `jigga-plugin-<name>.service`) so it runs now and
across reboots. Every step audited. Plugins bring their own runtimes (Node,
Go …) — JIGGA core stays stdlib+PyYAML; the boundary is the CLI and files.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from jigga.runtime.audit import append_event
from jigga.runtime.capabilities import (
    CapabilityManifest,
    load_capability_manifest,
    record_approval,
)
from jigga.runtime.capability_scanner import scan_capability
from jigga.runtime.service import (
    install_app_service,
    status_app_service,
    uninstall_app_service,
)

SetupRunner = Callable[[list[str], Path, dict[str, str]], "subprocess.CompletedProcess[str]"]


def _default_setup_runner(argv: list[str], cwd: Path, env: dict[str, str]) -> "subprocess.CompletedProcess[str]":
    import os

    return subprocess.run(argv, cwd=str(cwd), env={**os.environ, **env},
                          capture_output=True, text=True, timeout=600, check=False)


def plugins_dir(home: Path) -> Path:
    return Path(home) / "plugins"


def plugin_dir(home: Path, name: str) -> Path:
    return plugins_dir(home) / name


def load_app_manifest(manifest_path: Path) -> CapabilityManifest:
    capability = load_capability_manifest(manifest_path)
    if capability.type != "app":
        raise ValueError(
            f"{manifest_path} is type {capability.type!r}, not an app plugin — "
            "install capabilities with `jigga capabilities install`.")
    return capability


def _fetch_source(source: str, staging: Path,
                  run_fn: SetupRunner | None = None) -> Path:
    """Materialize the plugin source into `staging`: a local directory is
    copied; anything else is treated as a git URL and shallow-cloned."""
    if run_fn is None:  # resolved at call time so tests can patch the module attr
        run_fn = _default_setup_runner
    local = Path(source).expanduser()
    if local.is_dir():
        shutil.copytree(local, staging, ignore=shutil.ignore_patterns(
            ".git", "node_modules", ".next", "dist", "coverage"))
        return staging
    proc = run_fn(["git", "clone", "--depth", "1", source, str(staging)], Path.cwd(), {})
    if proc.returncode != 0:
        raise ValueError(f"git clone failed: {(proc.stderr or '').strip()[:500]}")
    shutil.rmtree(staging / ".git", ignore_errors=True)
    return staging


def _service_env(paths: Any, capability: CapabilityManifest) -> dict[str, str]:
    env = {"JIGGA_HOME": str(paths.home), "PATH": _path_env()}
    if capability.port:
        env["PORT"] = str(capability.port)
    env.update(capability.app_env or {})
    return env


def _path_env() -> str:
    import os

    return os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")


def install_plugin(paths: Any, source: str, *, service: bool = True,
                   run_fn: SetupRunner | None = None,
                   service_install_fn: Callable[..., dict] | None = None) -> dict[str, Any]:
    """Install an app plugin from a local dir or git URL. Returns a summary;
    raises ValueError on a bad source/manifest or failed setup (the partial
    install dir is removed so a retry starts clean)."""
    if run_fn is None:  # resolved at call time (def-time defaults defeat monkeypatching)
        run_fn = _default_setup_runner
    if service_install_fn is None:
        service_install_fn = install_app_service
    home = Path(paths.home)
    staging = plugins_dir(home) / ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    plugins_dir(home).mkdir(parents=True, exist_ok=True)

    _fetch_source(source, staging, run_fn)
    try:
        manifest_path = staging / "manifest.yaml"
        if not manifest_path.exists():
            raise ValueError(f"No manifest.yaml in {source!r} — a plugin needs a type: app manifest")
        capability = load_app_manifest(manifest_path)
        target = plugin_dir(home, capability.name)
        if target.exists():
            raise ValueError(f"Plugin {capability.name!r} is already installed — uninstall it first")
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    try:
        capability = load_app_manifest(target / "manifest.yaml")  # re-load with final source path
        report = scan_capability(capability, pack_dir=target)
        record_approval(paths.policies, capability)
        setup_results: list[dict[str, Any]] = []
        env = _service_env(paths, capability)
        for argv in capability.setup:
            proc = run_fn(argv, target, env)
            setup_results.append({"argv": argv, "returncode": proc.returncode,
                                  "stderr": (proc.stderr or "").strip()[:500]})
            if proc.returncode != 0:
                raise ValueError(f"setup step failed ({' '.join(argv)}): "
                                 f"{(proc.stderr or proc.stdout or '').strip()[:500]}")
        service_result = None
        if service:
            service_result = service_install_fn(
                capability.name, capability.run, cwd=target, env=env, logs_dir=paths.logs)
        append_event(paths.logs, "plugin.installed", name=capability.name,
                     version=capability.version, source=source, port=capability.port,
                     risk_findings=len(getattr(report, "findings", []) or []),
                     service=bool(service))
        return {
            "name": capability.name,
            "version": capability.version,
            "dir": str(target),
            "port": capability.port,
            "scan": report.to_dict() if hasattr(report, "to_dict") else None,
            "setup": setup_results,
            "service": service_result,
        }
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def list_plugins(paths: Any) -> list[dict[str, Any]]:
    """Installed plugins with manifest summary + live service status."""
    home = Path(paths.home)
    root = plugins_dir(home)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        manifest_path = directory / "manifest.yaml"
        if not manifest_path.exists():
            continue
        try:
            capability = load_app_manifest(manifest_path)
        except ValueError:
            continue
        status = status_app_service(capability.name)
        out.append({
            "name": capability.name,
            "version": capability.version,
            "summary": capability.summary,
            "port": capability.port,
            "dir": str(directory),
            "installed_service": bool(status.get("installed")),
            "running": bool(status.get("running")),
        })
    return out


def uninstall_plugin(paths: Any, name: str) -> dict[str, Any]:
    """Stop + remove the plugin's service unit and delete its directory."""
    home = Path(paths.home)
    target = plugin_dir(home, name)
    if not target.exists():
        raise ValueError(f"Plugin {name!r} is not installed")
    service_result = uninstall_app_service(name)
    shutil.rmtree(target, ignore_errors=True)
    append_event(paths.logs, "plugin.uninstalled", name=name)
    return {"name": name, "removed": True, "service": service_result}
