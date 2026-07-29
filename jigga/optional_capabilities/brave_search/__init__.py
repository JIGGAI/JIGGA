"""Brave Search API provider pack (#158): the wizard collects an API key into
`~/.jigga/secrets/brave_api_key` (0600, never in config) and selects the
provider. Free tier: https://brave.com/search/api/."""

from __future__ import annotations

from typing import Callable

from jigga.core.io import read_yaml, write_yaml

_HELP = """
Brave Search API gives clean JSON results (free tier ~2,000 queries/month).

  1. Create a key at https://brave.com/search/api/ (Data for Search plan).
  2. Paste it below — it is stored locally at secrets/brave_api_key (0600).
"""


def setup(paths, *, input_fn: Callable[[str], str] = input,
          print_fn: Callable[..., None] = print) -> int:
    print_fn("\n=== Brave Search setup ===")
    print_fn(_HELP)
    key = input_fn("Brave API key: ").strip()
    if not key:
        print_fn("An API key is required — aborting.")
        return 1
    from jigga.runtime.secrets_broker import set_secret

    path = set_secret(paths.home, "brave_api_key", key + "\n")
    config = read_yaml(paths.config) if paths.config.exists() else {}
    web = dict(config.get("web") or {})
    web["search_provider"] = "brave"
    config["web"] = web
    write_yaml(paths.config, config)
    print_fn(f"Key stored at {path} (0600). web.search now routes to the Brave API — "
             "grant agents network access to api.search.brave.com if they don't have it.")
    return 0
