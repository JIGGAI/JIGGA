"""Image generation pack (#150): the wizard collects an API key into the secrets
broker and selects the driver under `media.image` in config.

Default is Gemini — the nano-banana path the HMX marketing cadence runs on.
An OpenAI-compatible endpoint is the alternative for installs already pointed at
one. Note that JIGGA's *text* provider cannot serve this: `chatgpt_oauth` posts
to the Codex responses endpoint on a subscription token, so image generation is
a separately keyed provider.
"""

from __future__ import annotations

from typing import Callable

from jigga.core.io import read_yaml, write_yaml

_DRIVERS = {
    "1": {
        "provider": "gemini",
        "label": "Gemini (nano-banana)",
        "secret": "gemini_api_key",
        "model": "gemini-2.5-flash-image",
        "where": "https://aistudio.google.com/apikey",
        "host": "generativelanguage.googleapis.com",
    },
    "2": {
        "provider": "openai_compatible",
        "label": "OpenAI-compatible /images/generations",
        "secret": "media_image_api_key",
        "model": "gpt-image-1",
        "where": "your provider's dashboard",
        "host": "api.openai.com",
    },
}


def setup(paths, *, input_fn: Callable[[str], str] = input,
          print_fn: Callable[..., None] = print) -> int:
    print_fn("\n=== Image generation setup ===")
    print_fn("\nWhich provider should draw?")
    for key, spec in _DRIVERS.items():
        print_fn(f"  {key}. {spec['label']}")
    choice = (input_fn("Choose [1]: ").strip() or "1")
    spec = _DRIVERS.get(choice)
    if spec is None:
        print_fn(f"No provider {choice!r} — aborting.")
        return 1

    print_fn(f"\nGet a key from {spec['where']}. It is stored locally, never in config.")
    key = input_fn(f"{spec['label']} API key: ").strip()
    if not key:
        print_fn("An API key is required — aborting.")
        return 1

    # The model id is asked for rather than assumed: providers rename image
    # models often, and a default that drifts fails at generation time with a
    # provider error rather than here, where someone can fix it.
    model = input_fn(f"Model id [{spec['model']}]: ").strip() or spec["model"]

    from jigga.runtime.secrets_broker import set_secret

    path = set_secret(paths.home, spec["secret"], key + "\n")
    config = read_yaml(paths.config) if paths.config.exists() else {}
    media = dict(config.get("media") or {})
    media["image"] = {"provider": spec["provider"], "model": model,
                      "api_key_secret": spec["secret"]}
    config["media"] = media
    write_yaml(paths.config, config)
    print_fn(f"\nKey stored at {path} (0600). `media.generate_image` now routes to "
             f"{spec['label']} using {model}.")
    print_fn(f"Agents that use it need network access to {spec['host']}, and the "
             "`media.generate_image` grant in their `tools:`.")
    return 0
