"""SearXNG search-provider pack (#158): points `web.search` at a SearXNG
metasearch instance — self-hosted or public, JSON API, no key. The wizard
records the instance URL, selects the provider, and allowlists the host."""

from __future__ import annotations

import urllib.parse
from typing import Callable

from jigga.core.io import read_yaml, write_yaml

_HELP = """
SearXNG is an open-source metasearch server (aggregates Google/Bing/DDG...).
Self-host in one command:

  docker run -d --name searxng -p 8888:8080 searxng/searxng

then use http://localhost:8888 here (enable `json` in the instance's
settings.yml `search.formats` if you self-host), or paste a public
instance URL from https://searx.space.
"""


def setup(paths, *, input_fn: Callable[[str], str] = input,
          print_fn: Callable[..., None] = print) -> int:
    print_fn("\n=== SearXNG search setup ===")
    print_fn(_HELP)
    url = input_fn("SearXNG instance URL: ").strip().rstrip("/")
    host = urllib.parse.urlparse(url).hostname
    if not url or not host:
        print_fn("A full instance URL (http[s]://host[:port]) is required — aborting.")
        return 1
    config = read_yaml(paths.config) if paths.config.exists() else {}
    web = dict(config.get("web") or {})
    web["search_provider"] = "searxng"
    web["search_url"] = url
    config["web"] = web
    write_yaml(paths.config, config)
    print_fn(f"web.search now routes to {url} — grant agents network access to {host} "
             "(permissions.network.allow) if they don't have it.")
    return 0
