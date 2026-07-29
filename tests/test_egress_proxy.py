"""E3a/E3b: egress proxy allow/block (127.0.0.1-only traffic), audit events,
run_sandboxed env injection, MCP manifest wiring, doctor observed-vs-declared."""

from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.runtime.egress_proxy import EgressProxy, _host_allowed


def test_host_allowed_shapes() -> None:
    allowed = ["api.example.com", "*.docs.org", "https://hooks.example.net", "*"]
    assert _host_allowed("api.example.com", ["api.example.com"])
    assert not _host_allowed("evil-api.example.com", ["api.example.com"])
    assert _host_allowed("a.docs.org", ["*.docs.org"])
    assert _host_allowed("hooks.example.net", ["https://hooks.example.net"])  # URL entries
    assert _host_allowed("anything.at.all", allowed)  # explicit wildcard
    assert not _host_allowed("x", [])                 # empty allowlist = deny-all


@pytest.fixture()
def local_target():
    """A tiny local HTTP server the proxy can relay to (127.0.0.1 only)."""

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"target-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _via_proxy(proxy_port: int, url: str) -> tuple[int, bytes]:
    handler = urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy_port}"})
    opener = urllib.request.build_opener(handler)
    with opener.open(url, timeout=10) as response:
        return response.status, response.read()


def test_proxy_relays_allowed_and_blocks_unlisted(tmp_path: Path, local_target) -> None:
    paths = init_runtime(tmp_path, examples=True)
    proxy = EgressProxy(["127.0.0.1"], logs_dir=paths.logs, label="mcp:test")
    port = proxy.start()
    try:
        status, body = _via_proxy(port, f"http://127.0.0.1:{local_target}/x")
        assert status == 200 and body == b"target-ok"
        proxy2_decisions = proxy.decisions
        assert proxy2_decisions[-1] == {"host": "127.0.0.1", "allowed": True}
    finally:
        proxy.stop()

    blocked = EgressProxy([], logs_dir=paths.logs, label="mcp:test")  # deny-all
    port = blocked.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _via_proxy(port, f"http://127.0.0.1:{local_target}/x")
        assert excinfo.value.code == 403
    finally:
        blocked.stop()
    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "egress.allowed" and e["details"]["host"] == "127.0.0.1" for e in events)
    assert any(e["type"] == "egress.blocked" and e["details"]["label"] == "mcp:test" for e in events)


def test_run_sandboxed_injects_proxy_env_and_tears_down(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from jigga.runtime.sandbox import SandboxSpec, run_sandboxed

    captured = {}

    def fake_run(argv, **kw):
        captured["env"] = kw["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    import jigga.runtime.sandbox as mod

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    spec = SandboxSpec(command="echo", cwd=tmp_path, egress_allow=["api.example.com"])
    run_sandboxed(spec)
    env = captured["env"]
    assert env["HTTP_PROXY"].startswith("http://127.0.0.1:")
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] and env["NO_PROXY"] == "127.0.0.1,localhost"
    # No egress_allow → no proxy env.
    run_sandboxed(SandboxSpec(command="echo", cwd=tmp_path))
    assert "HTTP_PROXY" not in captured["env"]


def test_mcp_spec_carries_manifest_egress(tmp_path: Path, monkeypatch) -> None:

    from jigga.core.models import AgentConfig, WorkflowStep
    from jigga.runtime.capabilities import CapabilityManifest
    from jigga.runtime.handlers import _mcp_server_handler
    from jigga.runtime.runtime_context import RuntimeContext

    paths = init_runtime(tmp_path, examples=True)
    capability = CapabilityManifest(
        name="mcp-x", version="1", summary="s", actions=["x.run"], type="mcp_server",
        command="server-bin", permissions={"network": {"mode": "allow", "allow": ["api.example.com"]}})
    seen = {}

    import jigga.runtime.handlers as handlers_mod

    def fake_call(spec, tool_name, arguments=None):
        seen["spec"] = spec
        return {"ok": True}

    monkeypatch.setattr(handlers_mod, "call_mcp_tool", fake_call, raising=False)
    monkeypatch.setattr("jigga.runtime.handlers.call_mcp_tool", fake_call)
    agent = AgentConfig(id="a", name="A", role="r")
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    _mcp_server_handler(WorkflowStep(id="s", action="x.run"), capability, {}, {}, runtime)
    spec = seen["spec"]
    assert spec.egress_allow == ["api.example.com"]
    assert spec.label == "mcp:mcp-x" and spec.logs_dir == paths.logs


def test_doctor_surfaces_blocked_egress(tmp_path: Path) -> None:
    from jigga.core.paths import get_paths
    from jigga.runtime.audit import append_event
    from jigga.runtime.doctor import _check_egress

    paths = init_runtime(tmp_path, examples=True)
    jp = get_paths(tmp_path)
    assert _check_egress(jp).status == "ok"
    append_event(paths.logs, "egress.blocked", status="denied", host="attacker.example", label="mcp:x")
    check = _check_egress(jp)
    assert check.status == "warn" and "attacker.example" in check.detail
