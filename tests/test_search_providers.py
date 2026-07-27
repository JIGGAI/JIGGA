"""web.search provider dispatch (#158): searxng + brave parsing over mocked
HTTP, provider/host resolution, and the two install wizards."""

from __future__ import annotations

import json
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime import web


def _set_web(paths, **cfg) -> None:
    config = read_yaml(paths.config) if paths.config.exists() else {}
    config["web"] = {**(config.get("web") or {}), **cfg}
    write_yaml(paths.config, config)


def _mock_get(monkeypatch, body: str) -> list[str]:
    calls: list[str] = []

    def fake_get(url: str):
        calls.append(url)
        return 200, "application/json", body

    monkeypatch.setattr(web, "_get", fake_get)
    return calls


def test_provider_and_host_resolution(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    assert web.search_provider(paths.home) == "ddg_html"
    assert web.search_host(paths.home) == "html.duckduckgo.com"
    _set_web(paths, search_provider="searxng", search_url="http://searx.local:8888")
    assert web.search_host(paths.home) == "searx.local"
    _set_web(paths, search_provider="brave")
    assert web.search_host(paths.home) == "api.search.brave.com"


def test_searxng_parses_results(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _set_web(paths, search_provider="searxng", search_url="http://searx.local:8888")
    calls = _mock_get(monkeypatch, json.dumps({"results": [
        {"title": "Python Docs", "url": "https://docs.python.org/3/", "content": "Official docs."},
        {"title": "PEPs", "url": "https://peps.python.org/", "content": "Index."},
    ]}))
    result = web.search(paths.home, "python docs", max_results=1)
    assert calls[0].startswith("http://searx.local:8888/search?q=python+docs&format=json")
    assert result["provider"] == "searxng"
    assert result["results"] == [{"title": "Python Docs", "url": "https://docs.python.org/3/",
                                  "snippet": "Official docs."}]


def test_searxng_non_json_reports_settings_hint(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _set_web(paths, search_provider="searxng", search_url="http://searx.local")
    _mock_get(monkeypatch, "<html>html only</html>")
    result = web.search(paths.home, "q")
    assert result["results"] == [] and "format=json" in result["error"]


def test_brave_requires_key_then_parses(tmp_path: Path, monkeypatch) -> None:
    import pytest

    paths = init_runtime(tmp_path, examples=True)
    _set_web(paths, search_provider="brave")
    with pytest.raises(ValueError, match="brave-search"):
        web.search(paths.home, "q")

    web.brave_key_path(paths.home).parent.mkdir(parents=True, exist_ok=True)
    web.brave_key_path(paths.home).write_text("k-123\n", encoding="utf-8")
    seen = {}

    class _Resp:
        status = 200

        def read(self, _n):
            return json.dumps({"web": {"results": [
                {"title": "T", "url": "https://t.example", "description": "<b>snip</b>"}]}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=0):
        seen["token"] = request.headers.get("X-subscription-token")
        seen["url"] = request.full_url
        return _Resp()

    monkeypatch.setattr(web.urllib.request, "urlopen", fake_urlopen)
    result = web.search(paths.home, "test", max_results=3)
    assert seen["token"] == "k-123" and "count=3" in seen["url"]
    assert result["provider"] == "brave"
    assert result["results"] == [{"title": "T", "url": "https://t.example", "snippet": "snip"}]


def test_wizards_configure_providers(tmp_path: Path) -> None:
    from jigga.optional_capabilities.brave_search import setup as brave_setup
    from jigga.optional_capabilities.searxng import setup as searxng_setup

    paths = init_runtime(tmp_path, examples=True)
    assert searxng_setup(paths, input_fn=lambda _p: "http://searx.local:8888/",
                         print_fn=lambda *a, **k: None) == 0
    cfg = read_yaml(paths.config)["web"]
    assert cfg == {"search_provider": "searxng", "search_url": "http://searx.local:8888"}

    assert brave_setup(paths, input_fn=lambda _p: "key-9", print_fn=lambda *a, **k: None) == 0
    assert read_yaml(paths.config)["web"]["search_provider"] == "brave"
    key_path = web.brave_key_path(paths.home)
    assert key_path.read_text(encoding="utf-8").strip() == "key-9"
    assert (key_path.stat().st_mode & 0o777) == 0o600
    # Aborts: no URL / no key.
    assert searxng_setup(paths, input_fn=lambda _p: "", print_fn=lambda *a, **k: None) == 1
    assert brave_setup(paths, input_fn=lambda _p: " ", print_fn=lambda *a, **k: None) == 1


def test_packs_registered_for_install() -> None:
    from jigga.optional_capabilities import REGISTRY

    for name in ("brave-search", "searxng"):
        assert name in REGISTRY and REGISTRY[name].manifest_path.exists()
