"""Media generation — the `media` workflow node and its pluggable drivers.

A `media` node is the workflow's way of producing something that isn't text.
It dispatches `media.generate_image` like any other capability action, so it
inherits the whole existing chain: tool grants, risk gating, approvals, egress
policy, and the audit trail. Nothing here is a side door.

**Drivers are a seam, not a provider.** `IMAGE_DRIVERS` maps a driver name to a
callable; adding a provider is one entry plus one function. The default is
`gemini` — the nano-banana path the HMX marketing cadence already runs on — with
an OpenAI-compatible driver alongside it, both selected by
`media.image.provider`.

Worth knowing: JIGGA's *text* provider cannot serve this. `chatgpt_oauth` posts
to the Codex responses endpoint on a subscription token, and `openai_compatible`
posts to `/chat/completions`. Image generation is a separately configured,
API-keyed provider under `media.image`, which
`jigga capabilities install image-generation` sets up.

The handler returns base64 rather than writing the file itself. `execute_step`
owns artifact writing, and a handler reaching into the run directory would be
the only one that did.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Callable

from jigga.core.config import load_runtime_config
from jigga.core.models import WorkflowStep
from jigga.runtime.capabilities import CapabilityManifest
from jigga.runtime.runtime_context import RuntimeContext

DEFAULT_DRIVER = "gemini"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Overridable in config — pin whatever the install actually runs rather than
# inheriting a default that moves under you on a provider release.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-image"
DEFAULT_OPENAI_MODEL = "gpt-image-1"
DEFAULT_SIZE = "1024x1024"
DEFAULT_TIMEOUT_SECONDS = 120.0


class MediaConfigError(RuntimeError):
    """Media generation was asked for without a usable provider configured."""


class MediaDriverError(RuntimeError):
    """The provider was reached and refused, or answered with something else."""


def _image_config(home) -> dict[str, Any]:
    media = load_runtime_config(home).get("media") or {}
    image = media.get("image") if isinstance(media, dict) else None
    return image if isinstance(image, dict) else {}


def _api_key(home, config: dict[str, Any], default_name: str) -> tuple[str, str]:
    from jigga.runtime.secrets_broker import get_secret

    name = str(config.get("api_key_secret") or default_name)
    key = get_secret(home, name)
    if not key:
        raise MediaConfigError(
            f"no API key for image generation — expected secret {name!r}. "
            "Run `jigga capabilities install image-generation`."
        )
    return name, key


def _post_json(url: str, payload: bytes, headers: dict[str, str], timeout: float,
               *, what: str) -> dict[str, Any]:
    request = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — configured base_url
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise MediaDriverError(f"{what} failed: HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MediaDriverError(f"{what} could not reach {url.split('?')[0]}: {exc}") from exc


def _gemini_image(spec: dict[str, Any], *, home) -> dict[str, Any]:
    """Google Generative Language `:generateContent` — the nano-banana path.

    The image comes back as an `inlineData` part alongside any text the model
    felt like adding, so this walks the parts for the first inline image rather
    than assuming a position.
    """
    config = _image_config(home)
    _, api_key = _api_key(home, config, "gemini_api_key")
    model = str(spec.get("model") or config.get("model") or DEFAULT_GEMINI_MODEL)
    base_url = str(config.get("base_url") or GEMINI_BASE_URL).rstrip("/")
    body: dict[str, Any] = {"contents": [{"parts": [{"text": spec["prompt"]}]}]}
    generation_config = config.get("generation_config")
    if isinstance(generation_config, dict) and generation_config:
        body["generationConfig"] = generation_config
    parsed = _post_json(
        f"{base_url}/models/{model}:generateContent",
        json.dumps(body).encode("utf-8"),
        {"x-goog-api-key": api_key, "Content-Type": "application/json"},
        float(config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
        what="image generation",
    )
    candidates = parsed.get("candidates") or []
    parts = (candidates[0].get("content", {}).get("parts") or []) if candidates else []
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            return {"image_base64": inline["data"],
                    "media_type": inline.get("mimeType") or inline.get("mime_type") or "image/png",
                    "model": model, "driver": "gemini"}
    # Refusals and safety blocks arrive as a normal 200 with no image part —
    # surfacing the reason beats reporting an empty success.
    reason = (candidates[0].get("finishReason") if candidates else None) or parsed.get("promptFeedback")
    raise MediaDriverError(
        f"image generation returned no image data (model={model}, reason={reason!r})")


def _openai_compatible_image(spec: dict[str, Any], *, home) -> dict[str, Any]:
    """POST `{base_url}/images/generations` — for installs pointed at an
    OpenAI-compatible endpoint instead."""
    config = _image_config(home)
    _, api_key = _api_key(home, config, "media_image_api_key")
    model = str(spec.get("model") or config.get("model") or DEFAULT_OPENAI_MODEL)
    base_url = str(config.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    parsed = _post_json(
        f"{base_url}/images/generations",
        json.dumps({"model": model, "prompt": spec["prompt"], "n": 1,
                    "size": spec.get("size") or config.get("size") or DEFAULT_SIZE}).encode("utf-8"),
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        float(config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
        what="image generation",
    )
    entries = parsed.get("data") or []
    encoded = entries[0].get("b64_json") if entries else None
    if not encoded:
        # A URL-only reply is a real provider mode, but fetching it is a second
        # egress the capability never declared. Say so rather than quietly
        # reaching out again.
        raise MediaDriverError(
            "image generation returned no inline image data; JIGGA needs b64_json, "
            "not a URL (set the provider's response_format accordingly)")
    return {"image_base64": encoded, "media_type": "image/png",
            "model": model, "driver": "openai_compatible"}


# name -> driver. A new provider is one entry and one function.
IMAGE_DRIVERS: dict[str, Callable[..., dict[str, Any]]] = {
    "gemini": _gemini_image,
    "openai_compatible": _openai_compatible_image,
}


def media_handler(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime: RuntimeContext,
) -> Any:
    """`media.generate_image` — turn a prompt into an image.

    Returns `{image_base64, media_type, model, driver, prompt}`. The caller's
    `output:` decides where it lands: a step declaring `output: cover.png` gets
    the bytes written there by `execute_step`.
    """
    if step.action != "media.generate_image":
        raise ValueError(f"Unsupported media action: {step.action}")
    spec = resolved_input if isinstance(resolved_input, dict) else {"prompt": str(resolved_input)}
    prompt = spec.get("prompt") or spec.get("brief") or spec.get("description")
    if not prompt:
        raise ValueError("media.generate_image requires a 'prompt'")

    name = str(_image_config(runtime.home).get("provider") or DEFAULT_DRIVER)
    driver = IMAGE_DRIVERS.get(name)
    if driver is None:
        raise MediaConfigError(
            f"unknown image driver {name!r} (configured at media.image.provider). "
            f"Available: {', '.join(sorted(IMAGE_DRIVERS))}"
        )
    return {**driver({**spec, "prompt": str(prompt)}, home=runtime.home), "prompt": str(prompt)}


def binary_payload(output: Any) -> bytes | None:
    """Decode a handler result that carries binary content, else None.

    Lets `execute_step` write real bytes for an image artifact instead of
    serializing a base64 blob into a `.png` as JSON.
    """
    if not isinstance(output, dict):
        return None
    encoded = output.get("image_base64") or output.get("content_base64")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
