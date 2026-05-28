from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jigga.core.io import read_yaml
from jigga.core.models import AgentConfig
from jigga.runtime.audit import append_event


@dataclass(frozen=True)
class ModelProviderConfig:
    id: str
    kind: str = "dry_run"
    base_url: str | None = None
    api_key_env: str | None = None
    default_model: str | None = None
    timeout_seconds: int = 60


@dataclass(frozen=True)
class ModelProfileConfig:
    id: str
    primary: str
    fallback: list[str] = field(default_factory=list)
    max_cost_per_run: float | None = None
    allow_cloud: bool = True


@dataclass(frozen=True)
class ModelCallItem:
    role: str
    content: str
    id: str | None = None
    provider_item_id: str | None = None

    def to_provider_message(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class ModelCallRequest:
    agent_id: str
    role: str
    task: dict[str, Any]
    items: list[ModelCallItem]
    model: str | None = None
    model_profile: str | None = None
    dry_run: bool = False


@dataclass(frozen=True)
class ModelCallResult:
    status: str
    provider: str
    model: str
    content: str
    dry_run: bool
    error: str | None = None
    fallback_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DuplicateModelInputError(ValueError):
    pass


def load_model_config(home: Path) -> dict[str, Any]:
    config_file = home / "config.yaml"
    if not config_file.exists():
        return {}
    return read_yaml(config_file).get("models", {}) or {}


def _load_providers(home: Path) -> dict[str, ModelProviderConfig]:
    config = load_model_config(home)
    raw = config.get("providers", {}) or {}
    providers: dict[str, ModelProviderConfig] = {}
    for provider_id, provider_data in raw.items():
        providers[provider_id] = ModelProviderConfig(id=provider_id, **(provider_data or {}))
    if "dry_run" not in providers:
        providers["dry_run"] = ModelProviderConfig(id="dry_run", kind="dry_run", default_model="dry-run")
    return providers


def _load_profiles(home: Path) -> dict[str, ModelProfileConfig]:
    config = load_model_config(home)
    raw = config.get("profiles", {}) or {}
    profiles: dict[str, ModelProfileConfig] = {}
    for profile_id, profile_data in raw.items():
        profiles[profile_id] = ModelProfileConfig(id=profile_id, **(profile_data or {}))
    default_provider = (config.get("defaults", {}) or {}).get("provider", "dry_run")
    if "default" not in profiles:
        profiles["default"] = ModelProfileConfig(id="default", primary=default_provider)
    return profiles


def resolve_agent_model_profile(agent: AgentConfig) -> str:
    raw = agent.model or "default"
    if raw.startswith("profile:"):
        return raw.split(":", 1)[1] or "default"
    return "default"


def resolve_agent_model(agent: AgentConfig) -> str | None:
    raw = agent.model
    if not raw or raw.startswith("profile:"):
        return None
    return raw


def validate_unique_input_items(items: list[ModelCallItem]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        item_id = item.provider_item_id or item.id
        if not item_id:
            continue
        if item_id in seen:
            duplicates.append(item_id)
        seen.add(item_id)
    if duplicates:
        joined = ", ".join(sorted(set(duplicates)))
        raise DuplicateModelInputError(f"Duplicate model input item id(s): {joined}")


def build_task_model_request(agent: AgentConfig, task: dict[str, Any], dry_run: bool = False) -> ModelCallRequest:
    body = task.get("description") or task.get("title") or "No task description provided."
    items = [
        ModelCallItem(
            id="system",
            role="system",
            content=(
                f"You are {agent.name}. Role: {agent.role}. "
                "Complete the assigned task concisely and return the result only."
            ),
        ),
        ModelCallItem(
            id=f"task:{task['id']}",
            role="user",
            content=f"Task: {task.get('title')}\n\n{body}",
        ),
    ]
    return ModelCallRequest(
        agent_id=agent.id,
        role=agent.role,
        task=task,
        items=items,
        model=resolve_agent_model(agent),
        model_profile=resolve_agent_model_profile(agent),
        dry_run=dry_run,
    )


def _dry_run_result(request: ModelCallRequest, provider_id: str = "dry_run", model: str = "dry-run") -> ModelCallResult:
    title = request.task.get("title", "untitled task")
    return ModelCallResult(
        status="ok",
        provider=provider_id,
        model=model,
        content=f"Dry-run model response for task '{title}'. Configure a model provider to execute this with AI.",
        dry_run=True,
    )


def _call_openai_compatible(provider: ModelProviderConfig, request: ModelCallRequest, model: str) -> ModelCallResult:
    if not provider.api_key_env:
        raise ValueError(f"Provider {provider.id} is missing api_key_env")
    api_key = os.environ.get(provider.api_key_env)
    if not api_key:
        raise ValueError(f"Environment variable {provider.api_key_env} is not set")
    base_url = (provider.base_url or "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "messages": [item.to_provider_message() for item in request.items],
    }
    body = json.dumps(payload).encode("utf-8")
    http_request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=provider.timeout_seconds) as response:  # noqa: S310
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Model request failed: HTTP {exc.code}: {detail}") from exc
    content = data.get("choices", [{}])[0].get("message", {}).get("content")
    if not content:
        raise RuntimeError("Model response did not include choices[0].message.content")
    return ModelCallResult(status="ok", provider=provider.id, model=model, content=content, dry_run=False)


def _provider_order(profile: ModelProfileConfig) -> list[str]:
    return [profile.primary, *profile.fallback]


def call_model(home: Path, logs_dir: Path, request: ModelCallRequest) -> ModelCallResult:
    validate_unique_input_items(request.items)
    providers = _load_providers(home)
    profiles = _load_profiles(home)
    profile = profiles.get(request.model_profile or "default") or profiles["default"]

    if request.dry_run:
        result = _dry_run_result(request)
        append_event(logs_dir, "model.call", agent_id=request.agent_id, provider=result.provider, model=result.model, dry_run=True)
        return result

    failures: list[str] = []
    for index, provider_id in enumerate(_provider_order(profile)):
        provider = providers.get(provider_id)
        if provider is None:
            failures.append(f"{provider_id}: provider not configured")
            continue
        model = request.model or provider.default_model or provider.id
        try:
            if provider.kind == "dry_run":
                result = _dry_run_result(request, provider_id=provider.id, model=model)
            elif provider.kind == "openai_compatible":
                result = _call_openai_compatible(provider, request, model)
            else:
                raise ValueError(f"Unsupported provider kind: {provider.kind}")
            result = ModelCallResult(**{**result.to_dict(), "fallback_used": index > 0})
            append_event(
                logs_dir,
                "model.call",
                agent_id=request.agent_id,
                provider=result.provider,
                model=result.model,
                dry_run=result.dry_run,
                fallback_used=result.fallback_used,
            )
            return result
        except DuplicateModelInputError:
            raise
        except Exception as exc:  # provider fallback boundary
            failures.append(f"{provider_id}: {exc}")
            append_event(logs_dir, "model.call.failed", status="error", agent_id=request.agent_id, provider=provider_id, error=str(exc))

    return ModelCallResult(
        status="error",
        provider=profile.primary,
        model=request.model or "unknown",
        content="",
        dry_run=False,
        error="; ".join(failures) or "No model providers available",
    )
