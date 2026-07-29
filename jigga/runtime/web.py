"""Web capability — `web.fetch` + `web.search`, stdlib-only, allowlist-gated.

The first capability that lets an agent read the open web, so egress is
default-deny twice over:

1. **Config allowlist** — `web.allowed_domains` in `config.yaml` (exact host or
   `*.example.com`). No allowlist → every fetch is refused with a message that
   says how to enable it. The search host is implicitly allowed for
   `web.search` only.
2. **Per-agent network policy** — `evaluate_network(agent, host)` (the same
   Milestone-E-in-miniature egress check channel capabilities use), so a
   locked-down agent can't fetch even an allowlisted host.

Responses are text-extracted (tags stripped via stdlib HTMLParser) and
truncated — an agent reads pages, it doesn't mirror them. Fetched content is
untrusted input: the capability is `risk_level: medium`, so outside
`autonomous` mode every call is approval-gated.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config

DEFAULT_MAX_CHARS = 12_000
_MAX_DOWNLOAD_BYTES = 1_500_000
_TIMEOUT_SECONDS = 20
_SEARCH_HOST = "html.duckduckgo.com"
_USER_AGENT = "jigga/0.1 (+https://github.com/JIGGAI/JIGGA; local-first agent runtime)"


def allowed_domains(home: Path) -> list[str]:
    config = load_runtime_config(home)
    web = config.get("web") or {}
    domains = web.get("allowed_domains") or []
    return [str(d).lower().lstrip() for d in domains if str(d).strip()]


def _host_allowed(host: str, domains: list[str]) -> bool:
    host = host.lower()
    for domain in domains:
        if domain.startswith("*."):
            if host == domain[2:] or host.endswith(domain[1:]):
                return True
        elif host == domain:
            return True
    return False


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML degrades to raw text, never to a failed fetch
        return html
    return re.sub(r"\n{3,}", "\n\n", "\n".join(parser.parts))


def _get(url: str) -> tuple[int, str, str]:
    """(status, content_type, body_text) with a byte cap on the download."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 — scheme/host validated by caller
        raw = response.read(_MAX_DOWNLOAD_BYTES)
        content_type = response.headers.get("Content-Type", "")
        charset = response.headers.get_content_charset() or "utf-8"
        return response.status, content_type, raw.decode(charset, errors="replace")


def fetch(home: Path, url: str, *, max_chars: int = DEFAULT_MAX_CHARS,
          _domains: list[str] | None = None) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"web.fetch supports http/https only, got {parsed.scheme!r}")
    host = parsed.hostname or ""
    domains = _domains if _domains is not None else allowed_domains(home)
    if not _host_allowed(host, domains):
        raise PermissionError(
            f"Domain {host!r} is not in the web allowlist. Add it to config.yaml under "
            f"web.allowed_domains (e.g. jigga config set web.allowed_domains '[\"{host}\"]')."
        )
    try:
        status, content_type, body = _get(url)
    except urllib.error.HTTPError as exc:
        return {"source": "capability.web", "url": url, "status": exc.code,
                "error": f"HTTP {exc.code}: {exc.reason}"}
    if "html" in content_type:
        text = html_to_text(body)
    elif "json" in content_type:
        try:  # pretty-print so the model reads structure, not a wall of bytes
            text = json.dumps(json.loads(body), indent=1)[:max_chars]
        except ValueError:
            text = body
    else:
        text = body
    truncated = len(text) > max_chars
    return {
        "source": "capability.web", "url": url, "status": status,
        "content_type": content_type.split(";")[0].strip(),
        "text": text[:max_chars], "truncated": truncated,
    }


_RESULT_LINK = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_RESULT_SNIPPET = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAGS = re.compile(r"<[^>]+>")


def _clean_result_url(href: str) -> str:
    """DDG lite wraps result URLs as /l/?uddg=<encoded>; unwrap to the target."""
    parsed = urllib.parse.urlparse(href)
    if parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return target
    return href


def _web_config(home: Path) -> dict[str, Any]:
    return load_runtime_config(home).get("web") or {}


def search_provider(home: Path) -> str:
    """`web.search_provider` config: ddg_html (zero-config default), searxng
    (self-hosted/public instance, no key), or brave (API key). Installed via
    `jigga capabilities install searxng | brave-search` (#158)."""
    return str(_web_config(home).get("search_provider") or "ddg_html")


def search_host(home: Path) -> str:
    """The host `web.search` will egress to — what the handler's per-agent
    network-policy check evaluates."""
    provider = search_provider(home)
    if provider == "searxng":
        return urllib.parse.urlparse(str(_web_config(home).get("search_url") or "")).hostname or ""
    if provider == "brave":
        return "api.search.brave.com"
    return _SEARCH_HOST


def brave_key_path(home: Path) -> Path:
    return Path(home) / "secrets" / "brave_api_key"


def _payload(provider: str, query: str, results: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"source": "capability.web", "provider": provider,
                               "query": query, "results": results, **extra}
    if not results and "error" not in payload:
        payload["note"] = ("No results parsed — the search endpoint may have changed markup, "
                           "be unavailable, or be bot-blocking this host. Try web.fetch on a "
                           "known URL instead.")
    return payload


def _search_ddg(query: str, max_results: int) -> dict[str, Any]:
    url = f"https://{_SEARCH_HOST}/html/?q={urllib.parse.quote_plus(query)}"
    try:
        _status, _ctype, body = _get(url)
    except urllib.error.HTTPError as exc:
        return _payload("ddg_html", query, [], error=f"search endpoint returned HTTP {exc.code}")
    links = _RESULT_LINK.findall(body)
    snippets = [_TAGS.sub("", s).strip() for s in _RESULT_SNIPPET.findall(body)]
    results = []
    for index, (href, title_html) in enumerate(links[:max_results]):
        results.append({
            "title": _TAGS.sub("", title_html).strip(),
            "url": _clean_result_url(href),
            "snippet": snippets[index] if index < len(snippets) else "",
        })
    return _payload("ddg_html", query, results)


def _search_searxng(home: Path, query: str, max_results: int) -> dict[str, Any]:
    base = str(_web_config(home).get("search_url") or "").rstrip("/")
    if not base:
        raise ValueError("searxng provider needs web.search_url — re-run: "
                         "jigga capabilities install searxng")
    url = f"{base}/search?q={urllib.parse.quote_plus(query)}&format=json"
    try:
        _status, _ctype, body = _get(url)
        data = json.loads(body)
    except urllib.error.HTTPError as exc:
        return _payload("searxng", query, [], error=f"searxng instance returned HTTP {exc.code}")
    except ValueError:
        return _payload("searxng", query, [],
                        error="searxng instance returned non-JSON (is format=json enabled "
                              "in its settings.yml `search.formats`?)")
    results = [{"title": str(r.get("title", "")), "url": str(r.get("url", "")),
                "snippet": str(r.get("content", ""))}
               for r in (data.get("results") or [])[:max_results]]
    return _payload("searxng", query, results)


def _search_brave(home: Path, query: str, max_results: int) -> dict[str, Any]:
    from jigga.runtime.secrets_broker import get_secret

    key = (get_secret(home, "brave_api_key") or "").strip()
    if not key:
        raise ValueError("Brave provider needs an API key — run: "
                         "jigga capabilities install brave-search")
    url = (f"https://api.search.brave.com/res/v1/web/search"
           f"?q={urllib.parse.quote_plus(query)}&count={max_results}")
    request = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT, "Accept": "application/json", "X-Subscription-Token": key})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 — fixed https host
            data = json.loads(response.read(_MAX_DOWNLOAD_BYTES).decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        hint = " (invalid API key?)" if exc.code in (401, 403) else ""
        return _payload("brave", query, [], error=f"Brave API returned HTTP {exc.code}{hint}")
    results = [{"title": str(r.get("title", "")), "url": str(r.get("url", "")),
                "snippet": _TAGS.sub("", str(r.get("description", "")))}
               for r in ((data.get("web") or {}).get("results") or [])[:max_results]]
    return _payload("brave", query, results)


def search(home: Path, query: str, *, max_results: int = 8) -> dict[str, Any]:
    """Keyword web search via the configured provider (`web.search_provider`).
    All providers return the same shape and degrade with an explicit
    error/note — never fabricated results."""
    if not query or not query.strip():
        raise ValueError("web.search requires a query")
    query = query.strip()
    provider = search_provider(home)
    if provider == "searxng":
        return _search_searxng(home, query, max_results)
    if provider == "brave":
        return _search_brave(home, query, max_results)
    return _search_ddg(query, max_results)


def web_handler(step, capability, resolved_input, _memory_context, runtime) -> Any:
    """Dispatch `web.*`. Fetch enforces the config allowlist AND the executing
    agent's network policy for the target host; search only needs the (implicit)
    search-host grant plus the agent's network policy."""
    from jigga.runtime.policy import evaluate_network

    data = resolved_input if isinstance(resolved_input, dict) else {}
    if step.action == "web.fetch":
        url = str(data.get("url") or "").strip()
        if not url:
            raise ValueError("web.fetch requires input.url")
        host = urllib.parse.urlparse(url).hostname or ""
        if runtime.agent is not None:
            decision = evaluate_network(runtime.agent, host)
            if decision.status != "allow":
                raise PermissionError(decision.reason or f"Network egress to {host!r} denied by agent policy.")
        max_chars = int(data.get("max_chars") or DEFAULT_MAX_CHARS)
        return fetch(runtime.home, url, max_chars=max_chars)
    if step.action == "web.search":
        if runtime.agent is not None:
            decision = evaluate_network(runtime.agent, search_host(runtime.home))
            if decision.status != "allow":
                raise PermissionError(decision.reason or "Network egress denied by agent policy.")
        return search(runtime.home, str(data.get("query") or ""),
                      max_results=int(data.get("max_results") or 8))
    raise ValueError(f"Unknown web action: {step.action}")
