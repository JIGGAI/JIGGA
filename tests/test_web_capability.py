"""web.fetch / web.search: allowlist gating, text extraction, agent network
policy enforcement, and search-result parsing — all with mocked HTTP."""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime import web
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.runtime_context import RuntimeContext


def _allow_domains(paths, domains: list[str]) -> None:
    write_yaml(paths.config, {"web": {"allowed_domains": domains}})


def _mock_get(monkeypatch, body: str, content_type: str = "text/html", status: int = 200) -> list[str]:
    calls: list[str] = []

    def fake_get(url: str):
        calls.append(url)
        return status, content_type, body

    monkeypatch.setattr(web, "_get", fake_get)
    return calls


def test_host_allowlist_exact_and_wildcard() -> None:
    domains = ["example.com", "*.docs.org"]
    assert web._host_allowed("example.com", domains)
    assert not web._host_allowed("evil-example.com", domains)
    assert not web._host_allowed("sub.example.com", domains)
    assert web._host_allowed("api.docs.org", domains)
    assert web._host_allowed("docs.org", domains)


def test_fetch_refuses_unallowlisted_domain(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _mock_get(monkeypatch, "<p>hi</p>")
    with pytest.raises(PermissionError, match="not in the web allowlist"):
        web.fetch(paths.home, "https://example.com/page")


def test_fetch_extracts_text_and_truncates(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _allow_domains(paths, ["example.com"])
    _mock_get(monkeypatch, "<html><script>var x=1;</script><body><h1>Title</h1><p>Body text</p></body></html>")
    result = web.fetch(paths.home, "https://example.com/page")
    assert result["status"] == 200
    assert "Title" in result["text"] and "Body text" in result["text"]
    assert "var x=1" not in result["text"]

    _mock_get(monkeypatch, "<p>" + "x" * 500 + "</p>")
    result = web.fetch(paths.home, "https://example.com/page", max_chars=100)
    assert result["truncated"] is True and len(result["text"]) == 100


def test_fetch_rejects_non_http_schemes(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    with pytest.raises(ValueError, match="http/https only"):
        web.fetch(paths.home, "file:///etc/passwd")


_DDG_HTML = """
<div class="result">
<a rel="nofollow" class="result__a" href="/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=x">Python <b>Docs</b></a>
<a class="result__snippet" href="#">The official <b>Python</b> documentation.</a>
</div>
<div class="result">
<a rel="nofollow" class="result__a" href="https://peps.python.org/">PEP Index</a>
<a class="result__snippet" href="#">Index of Python Enhancement Proposals.</a>
</div>
"""


def test_search_parses_results_and_unwraps_redirects(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    calls = _mock_get(monkeypatch, _DDG_HTML)
    result = web.search(paths.home, "python docs", max_results=5)
    assert calls[0].startswith("https://html.duckduckgo.com/html/?q=python+docs")
    assert [r["url"] for r in result["results"]] == ["https://docs.python.org/3/", "https://peps.python.org/"]
    assert result["results"][0]["title"] == "Python Docs"
    assert "official" in result["results"][0]["snippet"]


def test_search_empty_markup_notes_instead_of_guessing(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _mock_get(monkeypatch, "<html>layout changed</html>")
    result = web.search(paths.home, "anything")
    assert result["results"] == [] and "note" in result


def _runtime(paths, agent: AgentConfig) -> RuntimeContext:
    return RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                          sessions_dir=paths.home / "sessions")


def test_handler_enforces_agent_network_policy(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _allow_domains(paths, ["example.com"])
    _mock_get(monkeypatch, "<p>ok</p>")
    step = WorkflowStep(id="s", action="web.fetch")
    denied = AgentConfig(id="a", name="A", role="r")  # no network permission → deny
    with pytest.raises(PermissionError):
        web.web_handler(step, None, {"url": "https://example.com/"}, {}, _runtime(paths, denied))
    granted = AgentConfig(id="a", name="A", role="r", permissions={"network": {"mode": "allow"}})
    result = web.web_handler(step, None, {"url": "https://example.com/"}, {}, _runtime(paths, granted))
    assert result["status"] == 200


def test_web_capability_registered_medium_risk() -> None:
    registry = CapabilityRegistry.load()
    capability = registry.resolve_action("web.fetch")
    assert capability is not None and capability.name == "web"
    assert capability.risk_level == "medium"
    assert registry.resolve_action("web.search") is capability
