"""Media nodes — image generation as a capability, drivers as a seam.

HMX's marketing cadence runs a `media-image` node on nano-banana. JIGGA had no
media node type and no image path at all, which made tenant A's third content
workflow unrunnable.

Everything here goes through the ordinary capability chain — tool grants, risk
gating, egress policy, audit — so a picture costs the same scrutiny as any other
paid third-party call.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_workflows
from jigga.core.io import read_yaml, write_yaml
from jigga.core.models import WorkflowStep
from jigga.runtime import media as media_module
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.media import (
    IMAGE_DRIVERS,
    MediaConfigError,
    MediaDriverError,
    binary_payload,
    media_handler,
)
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.workflow import plan_workflow, run_workflow

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n-not-a-real-png-but-bytes").decode()


def _agent(paths, *, tools=("media.generate_image",)) -> RuntimeContext:
    write_yaml(paths.agents / "designer.yaml", {
        "id": "designer", "name": "Designer", "role": "Makes pictures.",
        "memory_scope": "task_only", "permission_mode": "autonomous",
        "tools": list(tools),
        "permissions": {"network": {"mode": "allow"}, "shell": {"mode": "deny"},
                        "filesystem": {"allow": [f"{paths.home}/**"]}},
    })
    agent = load_agents(paths.agents)["designer"]
    return RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                          sessions_dir=paths.home / "sessions")


def _configure(paths, **overrides) -> None:
    config = read_yaml(paths.config)
    config["media"] = {"image": {"provider": "gemini", "model": "m",
                                 "api_key_secret": "gemini_api_key", **overrides}}
    write_yaml(paths.config, config)
    from jigga.runtime.secrets_broker import set_secret
    set_secret(paths.home, "gemini_api_key", "k\n")


def _step(**kw) -> WorkflowStep:
    kw.setdefault("id", "draw")
    kw.setdefault("action", "media.generate_image")
    return WorkflowStep(**kw)


# --- registration -----------------------------------------------------------


def test_media_is_a_registered_capability(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    registry = CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                       approvals_dir=paths.policies)
    capability = registry.resolve_action("media.generate_image")
    assert capability is not None
    assert capability.handler == "runtime.media"
    # Network egress plus real spend per call — it is not a low-risk action.
    assert capability.risk_level == "medium"


def test_media_is_a_valid_node_type() -> None:
    from jigga.runtime.workflow_engine import NODE_TYPES, _NODE_TYPE_ACTIONS

    assert "media" in NODE_TYPES
    assert _NODE_TYPE_ACTIONS["media"] == "media.generate_image"


# --- the driver seam --------------------------------------------------------


def test_the_default_driver_is_gemini() -> None:
    from jigga.runtime.media import DEFAULT_DRIVER

    assert DEFAULT_DRIVER == "gemini"
    assert set(IMAGE_DRIVERS) >= {"gemini", "openai_compatible"}


def test_gemini_driver_extracts_the_inline_image(tmp_path: Path) -> None:
    """The image arrives as an inlineData part beside whatever text the model
    added, so position can't be assumed."""
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    _configure(paths)
    reply = {"candidates": [{"content": {"parts": [
        {"text": "Here you go!"},
        {"inlineData": {"mimeType": "image/png", "data": PNG}},
    ]}}]}
    with patch.object(media_module, "_post_json", lambda *a, **k: reply):
        out = media_handler(_step(), None, {"prompt": "a barber pole"}, {}, runtime)
    assert out["image_base64"] == PNG
    assert out["media_type"] == "image/png"
    assert out["prompt"] == "a barber pole"
    assert out["driver"] == "gemini"


def test_a_safety_block_is_reported_not_swallowed(tmp_path: Path) -> None:
    """Refusals arrive as a normal 200 with no image part. Reporting an empty
    success would be the worst possible answer."""
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    _configure(paths)
    reply = {"candidates": [{"finishReason": "SAFETY", "content": {"parts": [{"text": "no"}]}}]}
    with patch.object(media_module, "_post_json", lambda *a, **k: reply):
        with pytest.raises(MediaDriverError, match="SAFETY"):
            media_handler(_step(), None, {"prompt": "x"}, {}, runtime)


def test_a_missing_api_key_says_what_to_run(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    config = read_yaml(paths.config)
    config["media"] = {"image": {"provider": "gemini"}}
    write_yaml(paths.config, config)
    with pytest.raises(MediaConfigError, match="capabilities install image-generation"):
        media_handler(_step(), None, {"prompt": "x"}, {}, runtime)


def test_an_unknown_driver_lists_the_real_ones(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    _configure(paths, provider="nonesuch")
    with pytest.raises(MediaConfigError, match="gemini"):
        media_handler(_step(), None, {"prompt": "x"}, {}, runtime)


def test_a_prompt_is_required(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    _configure(paths)
    with pytest.raises(ValueError, match="requires a 'prompt'"):
        media_handler(_step(), None, {"size": "1024x1024"}, {}, runtime)


def test_openai_compatible_driver_refuses_a_url_only_reply(tmp_path: Path) -> None:
    """Fetching the URL would be a second egress the capability never declared."""
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    _configure(paths, provider="openai_compatible", api_key_secret="gemini_api_key")
    with patch.object(media_module, "_post_json",
                      lambda *a, **k: {"data": [{"url": "https://example.com/i.png"}]}):
        with pytest.raises(MediaDriverError, match="b64_json"):
            media_handler(_step(), None, {"prompt": "x"}, {}, runtime)


# --- binary artifacts -------------------------------------------------------


def test_binary_payload_decodes_only_real_base64() -> None:
    assert binary_payload({"image_base64": PNG}) == base64.b64decode(PNG)
    assert binary_payload({"image_base64": "not base64!!"}) is None
    assert binary_payload({"summary": "text"}) is None
    assert binary_payload("a string") is None


def test_an_image_artifact_is_written_as_bytes(tmp_path: Path) -> None:
    """Without this a `output: cover.png` gets a base64 blob serialized into it
    as JSON — a .png that isn't one."""
    paths = init_runtime(tmp_path)
    _agent(paths)
    write_yaml(paths.workflows / "art.yaml", {
        "id": "art", "name": "art", "status": "active",
        "steps": [{"id": "draw", "agent": "designer", "action": "media.generate_image",
                   "input": {"prompt": "a barber pole"}, "output": "cover.png",
                   "approval": "not_required"}],
    })
    _configure(paths)
    reply = {"candidates": [{"content": {"parts": [{"inlineData": {"data": PNG}}]}}]}
    with patch.object(media_module, "_post_json", lambda *a, **k: reply):
        result = run_workflow(paths, "art")
    assert result["status"] == "completed"
    written = Path(result["run_dir"]) / "cover.png"
    assert written.read_bytes() == base64.b64decode(PNG)
    assert not written.read_bytes().startswith(b"{")      # not JSON


def test_non_binary_artifacts_are_unaffected(tmp_path: Path, grant) -> None:
    paths = init_runtime(tmp_path, examples=True)
    grant(paths, "daily_briefing_agent", "calendar.list_events")
    write_yaml(paths.workflows / "cal.yaml", {
        "id": "cal", "name": "cal", "status": "active",
        "steps": [{"id": "read", "agent": "daily_briefing_agent",
                   "action": "calendar.list_events", "input": {}, "output": "events.json",
                   "approval": "not_required"}],
    })
    result = run_workflow(paths, "cal")
    assert result["status"] == "completed"
    assert json.loads((Path(result["run_dir"]) / "events.json").read_text())


# --- it inherits the whole gate ---------------------------------------------


def test_an_ungranted_agent_cannot_draw(tmp_path: Path) -> None:
    """Media is a capability, not a side door — the grant boundary applies."""
    paths = init_runtime(tmp_path)
    _agent(paths, tools=())
    write_yaml(paths.workflows / "art.yaml", {
        "id": "art", "name": "art", "status": "active",
        "steps": [{"id": "draw", "agent": "designer", "action": "media.generate_image",
                   "input": {"prompt": "x"}, "approval": "not_required"}],
    })
    plan = plan_workflow(load_workflows(paths.workflows)["art"], load_agents(paths.agents),
                         registry=CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                                          approvals_dir=paths.policies))
    assert plan["can_run"] is False
    assert plan["steps"][0]["policy"]["permission"] == "tools.grant"


def test_a_media_node_runs_in_a_v2_graph(tmp_path: Path) -> None:
    from jigga.runtime.workflow_engine import run_workflow_v2

    paths = init_runtime(tmp_path)
    _agent(paths)
    _configure(paths)
    write_yaml(paths.workflows / "dag.yaml", {
        "id": "dag", "name": "dag", "status": "active",
        "nodes": [{"id": "draw", "type": "media", "agent": "designer",
                   "input": {"prompt": "a barber pole"}, "output": "cover.png"}],
        "edges": [],
    })
    reply = {"candidates": [{"content": {"parts": [{"inlineData": {"data": PNG}}]}}]}
    with patch.object(media_module, "_post_json", lambda *a, **k: reply):
        record = run_workflow_v2(paths, load_workflows(paths.workflows)["dag"])
    assert record["status"] == "completed"
    assert record["outputs"]["draw"]["media_type"]


# --- the install pack -------------------------------------------------------


def test_the_pack_configures_the_driver_and_stores_the_key(tmp_path: Path) -> None:
    from jigga.optional_capabilities.image_generation import setup
    from jigga.runtime.secrets_broker import get_secret

    paths = init_runtime(tmp_path)
    answers = iter(["1", "secret-key", ""])          # gemini, key, default model
    assert setup(paths, input_fn=lambda _p: next(answers, ""),
                 print_fn=lambda *a, **k: None) == 0
    image = read_yaml(paths.config)["media"]["image"]
    assert image["provider"] == "gemini"
    assert image["model"] == "gemini-2.5-flash-image"
    assert get_secret(paths.home, "gemini_api_key").strip() == "secret-key"
    # The key is never written into config.
    assert "secret-key" not in paths.config.read_text()


def test_the_pack_refuses_without_a_key(tmp_path: Path) -> None:
    from jigga.optional_capabilities.image_generation import setup

    paths = init_runtime(tmp_path)
    answers = iter(["1", "", ""])
    assert setup(paths, input_fn=lambda _p: next(answers, ""),
                 print_fn=lambda *a, **k: None) == 1
    assert "media" not in read_yaml(paths.config)


def test_the_pack_is_installable_by_name(tmp_path: Path) -> None:
    from jigga.optional_capabilities import REGISTRY

    assert "image-generation" in REGISTRY
    assert REGISTRY["image-generation"].manifest_path.is_file()
