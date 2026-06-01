from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from jigga.core.io import read_yaml
from jigga.core.models import AgentConfig
from jigga.runtime.audit import append_event, new_id
from jigga.runtime.cost import (
    WARN_FRACTION,
    agent_budget,
    estimate_tokens,
    load_pricing,
    price_call,
)
from jigga.runtime.spend_ledger import window_spend


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
class ModelToolCall:
    """A model's request to invoke one tool (== a JIGGA capability action).

    `name` is the capability action name (e.g. `telegram.send_message`);
    `arguments` is the parsed input dict the model wants to pass. `id`
    correlates the call with its result message on the next turn.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True)
class ModelCallItem:
    role: str
    content: str
    id: str | None = None
    provider_item_id: str | None = None
    # For role="tool" result messages: which tool call this answers.
    tool_call_id: str | None = None
    # For role="assistant" tool-call turns: the calls the model made.
    tool_calls: list[ModelToolCall] | None = None

    def to_provider_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            message["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in self.tool_calls
            ]
        return message


@dataclass(frozen=True)
class ModelCallRequest:
    agent_id: str
    role: str
    task: dict[str, Any]
    items: list[ModelCallItem]
    model: str | None = None
    model_profile: str | None = None
    dry_run: bool = False
    # Tool schemas (OpenAI function-tool format) the model may call. None =
    # plain completion, no tools offered.
    tools: list[dict[str, Any]] | None = None
    # Test/scripting hook: when the dry_run provider handles this request and
    # this is set, it returns these as the model's tool calls instead of text.
    # Lets the agent loop (PR C) be exercised deterministically without a real
    # LLM. Ignored by real providers.
    dry_run_tool_calls: list[ModelToolCall] | None = None


@dataclass(frozen=True)
class ModelCallResult:
    status: str
    provider: str
    model: str
    content: str
    dry_run: bool
    error: str | None = None
    fallback_used: bool = False
    # Non-empty → this is a tool-call turn; `content` may be empty. Empty →
    # `content` is the model's final answer.
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    # Token usage and priced cost. Real providers report exact usage; dry-run
    # estimates it. cost_usd is filled in by call_model from the config rate
    # table (0.0 when the model has no configured price).
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

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


def _prompt_tokens(request: ModelCallRequest) -> int:
    return estimate_tokens(" ".join(item.content or "" for item in request.items))


def _dry_run_result(request: ModelCallRequest, provider_id: str = "dry_run", model: str = "dry-run") -> ModelCallResult:
    input_tokens = _prompt_tokens(request)
    if request.dry_run_tool_calls:
        # Scripted tool-call turn — used by tests and by the agent loop to run
        # deterministically without a real model.
        return ModelCallResult(
            status="ok",
            provider=provider_id,
            model=model,
            content="",
            dry_run=True,
            tool_calls=list(request.dry_run_tool_calls),
            input_tokens=input_tokens,
        )
    title = request.task.get("title", "untitled task")
    content = f"Dry-run model response for task '{title}'. Configure a model provider to execute this with AI."
    return ModelCallResult(
        status="ok",
        provider=provider_id,
        model=model,
        content=content,
        dry_run=True,
        input_tokens=input_tokens,
        output_tokens=estimate_tokens(content),
    )


def _call_openai_compatible(provider: ModelProviderConfig, request: ModelCallRequest, model: str) -> ModelCallResult:
    if not provider.api_key_env:
        raise ValueError(f"Provider {provider.id} is missing api_key_env")
    api_key = os.environ.get(provider.api_key_env)
    if not api_key:
        raise ValueError(f"Environment variable {provider.api_key_env} is not set")
    base_url = (provider.base_url or "https://api.openai.com/v1").rstrip("/")
    payload: dict[str, Any] = {
        "model": model,
        "messages": [item.to_provider_message() for item in request.items],
    }
    if request.tools:
        payload["tools"] = request.tools
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
    message = data.get("choices", [{}])[0].get("message", {})
    tool_calls = _parse_tool_calls(message.get("tool_calls"))
    content = message.get("content")
    if not content and not tool_calls:
        raise RuntimeError("Model response included neither content nor tool_calls")
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or 0) or _prompt_tokens(request)
    output_tokens = int(usage.get("completion_tokens") or 0) or estimate_tokens(content or "")
    return ModelCallResult(
        status="ok",
        provider=provider.id,
        model=model,
        content=content or "",
        dry_run=False,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


CHATGPT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
# Codex masquerade headers — the ChatGPT backend only serves clients that look
# like the Codex CLI. Verified against codex 0.135.0 + the live backend.
_CODEX_ORIGINATOR = "codex_cli_rs"
_CODEX_UA = "codex_cli_rs/0.135.0"


def _build_responses_payload(request: ModelCallRequest, model: str) -> dict[str, Any]:
    """Map a ModelCallRequest into a ChatGPT/Codex Responses-API body.

    System items become `instructions`; user/assistant/tool items become Responses
    `input` items; assistant tool-call turns become `function_call` items and tool
    results become `function_call_output` items. Tools are flattened from the
    chat-completions `{type, function:{...}}` shape to the Responses `{type, name,
    description, parameters}` shape.
    """
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for item in request.items:
        if item.role == "system":
            if item.content:
                instructions.append(item.content)
            continue
        if item.role == "tool":
            input_items.append(
                {"type": "function_call_output", "call_id": item.tool_call_id, "output": item.content or ""}
            )
            continue
        if item.role == "assistant" and item.tool_calls:
            if item.content:
                input_items.append({"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": item.content}]})
            for call in item.tool_calls:
                input_items.append({"type": "function_call", "call_id": call.id,
                                    "name": call.name, "arguments": json.dumps(call.arguments)})
            continue
        text_type = "input_text" if item.role == "user" else "output_text"
        input_items.append({"type": "message", "role": item.role,
                            "content": [{"type": text_type, "text": item.content or ""}]})

    payload: dict[str, Any] = {
        "model": model,
        "instructions": "\n\n".join(instructions),
        "input": input_items,
        "store": False,          # the ChatGPT backend requires stateless requests
        "stream": True,
        "reasoning": {"effort": "low"},
    }
    if request.tools:
        payload["tools"] = [
            {"type": "function", "name": fn.get("name"), "description": fn.get("description"),
             "parameters": fn.get("parameters")}
            for tool in request.tools if (fn := tool.get("function", tool))
        ]
    return payload


def _int_or_zero(value: Any) -> int:
    """Token counts from a provider may be missing/malformed; coerce to int, 0 on
    failure — usage parsing must not crash the call."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_responses_stream(lines: Any) -> dict[str, Any]:
    """Parse a Responses-API SSE byte/line stream into a normalized result.

    Items arrive on `response.output_item.done` (this backend leaves
    `response.completed.output` empty); usage arrives on `response.completed`.
    Returns {content, tool_calls, input_tokens, output_tokens}.
    """
    content_parts: list[str] = []
    tool_calls: list[ModelToolCall] = []
    input_tokens = output_tokens = 0
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                content = item.get("content")
                for part in content if isinstance(content, list) else []:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text") and part.get("text"):
                        content_parts.append(part["text"])
            elif item.get("type") == "function_call":
                try:
                    arguments = json.loads(item.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    arguments = {"_raw": item.get("arguments")}
                tool_calls.append(ModelToolCall(
                    id=str(item.get("call_id") or item.get("id") or item.get("name")),
                    name=str(item.get("name")), arguments=arguments))
        elif etype in ("response.completed", "response.done"):
            response = event.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            input_tokens = _int_or_zero(usage.get("input_tokens"))
            output_tokens = _int_or_zero(usage.get("output_tokens"))
    return {"content": "".join(content_parts), "tool_calls": tool_calls,
            "input_tokens": input_tokens, "output_tokens": output_tokens}


def _call_chatgpt_oauth(provider: ModelProviderConfig, request: ModelCallRequest, model: str, home: Path) -> ModelCallResult:
    """Call the ChatGPT/Codex backend on a subscription OAuth token (no API key)."""
    from jigga.runtime.chatgpt_auth import load_credentials  # local import: optional dependency path

    creds = load_credentials(home=home)
    payload = json.dumps(_build_responses_payload(request, model)).encode("utf-8")

    def _post(access_token: str, account_id: str | None):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "OpenAI-Beta": "responses=experimental",
            "originator": _CODEX_ORIGINATOR,
            "User-Agent": _CODEX_UA,
            "session_id": new_id("session").split("_", 1)[1],
            "accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        if account_id:
            headers["chatgpt-account-id"] = account_id
        http_request = urllib.request.Request(CHATGPT_CODEX_URL, data=payload, headers=headers, method="POST")
        return urllib.request.urlopen(http_request, timeout=provider.timeout_seconds)  # noqa: S310

    try:
        response = _post(creds.access_token, creds.account_id)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):  # token expired/rejected → refresh once and retry
            creds.force_refresh()
            try:
                response = _post(creds.access_token, creds.account_id)
            except urllib.error.HTTPError as retry_exc:
                detail = retry_exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"ChatGPT request failed after refresh: HTTP {retry_exc.code}: {detail}") from retry_exc
        else:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ChatGPT request failed: HTTP {exc.code}: {detail}") from exc

    with response:
        parsed = parse_responses_stream(response)
    if not parsed["content"] and not parsed["tool_calls"]:
        raise RuntimeError("ChatGPT response included neither content nor tool_calls")
    return ModelCallResult(
        status="ok", provider=provider.id, model=model, content=parsed["content"], dry_run=False,
        tool_calls=parsed["tool_calls"],
        input_tokens=parsed["input_tokens"] or _prompt_tokens(request),
        output_tokens=parsed["output_tokens"] or estimate_tokens(parsed["content"]),
    )


def _parse_tool_calls(raw: Any) -> list[ModelToolCall]:
    """Parse OpenAI `message.tool_calls` into ModelToolCall. Tolerates missing
    or malformed argument JSON (falls back to an empty/raw dict) so a single
    bad call doesn't crash the turn."""
    if not isinstance(raw, list):
        return []
    calls: list[ModelToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not name:
            continue
        raw_args = function.get("arguments")
        if isinstance(raw_args, dict):
            arguments = raw_args
        else:
            try:
                arguments = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                arguments = {"_raw": raw_args}
        calls.append(ModelToolCall(id=str(item.get("id") or name), name=str(name), arguments=arguments))
    return calls


def _provider_order(profile: ModelProfileConfig) -> list[str]:
    return [profile.primary, *profile.fallback]


def _budget_spent_before(home: Path, logs_dir: Path, agent_id: str, budget: dict[str, Any] | None) -> float:
    if not budget:
        return 0.0
    # Read from the derived ledger (incremental tail over the audit log) instead
    # of re-scanning the whole log on every call. The ledger self-heals from the
    # log, which stays the source of truth.
    return window_spend(home, logs_dir, agent_id, window=budget.get("window"))


def _budget_hard_stop(
    logs_dir: Path,
    request: ModelCallRequest,
    profile: ModelProfileConfig,
    budget: dict[str, Any] | None,
    spent_before: float,
) -> ModelCallResult | None:
    """Deny the call when the agent has already spent its whole cap. Returns an
    error result to short-circuit, or None to proceed."""
    if not budget:
        return None
    limit = float(budget["limit_usd"])
    if spent_before < limit:
        return None
    append_event(logs_dir, "budget.exceeded", status="deny", agent_id=request.agent_id,
                 spent_usd=round(spent_before, 6), limit_usd=limit)
    return ModelCallResult(
        status="error",
        provider=profile.primary,
        model=request.model or "unknown",
        content="",
        dry_run=request.dry_run,
        error=f"budget_exceeded: agent {request.agent_id} spent ${spent_before:.4f} of ${limit:.2f} cap",
    )


def _emit_and_return(
    logs_dir: Path,
    request: ModelCallRequest,
    result: ModelCallResult,
    budget: dict[str, Any] | None,
    spent_before: float,
) -> ModelCallResult:
    append_event(
        logs_dir,
        "model.call",
        agent_id=request.agent_id,
        provider=result.provider,
        model=result.model,
        dry_run=result.dry_run,
        fallback_used=result.fallback_used,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )
    # Soft-warn exactly once, on the call that crosses the threshold.
    if budget:
        limit = float(budget["limit_usd"])
        spent_after = spent_before + result.cost_usd
        if limit > 0 and spent_before < WARN_FRACTION * limit <= spent_after:
            append_event(logs_dir, "budget.warning", status="ask", agent_id=request.agent_id,
                         spent_usd=round(spent_after, 6), limit_usd=limit,
                         fraction=round(spent_after / limit, 4))
    return result


def call_model(home: Path, logs_dir: Path, request: ModelCallRequest) -> ModelCallResult:
    validate_unique_input_items(request.items)
    providers = _load_providers(home)
    profiles = _load_profiles(home)
    profile = profiles.get(request.model_profile or "default") or profiles["default"]
    pricing = load_pricing(home)
    budget = agent_budget(home, request.agent_id)
    spent_before = _budget_spent_before(home, logs_dir, request.agent_id, budget)

    denied = _budget_hard_stop(logs_dir, request, profile, budget, spent_before)
    if denied is not None:
        return denied

    if request.dry_run:
        base = _dry_run_result(request)
        result = replace(base, cost_usd=price_call(base.model, base.input_tokens, base.output_tokens, pricing))
        return _emit_and_return(logs_dir, request, result, budget, spent_before)

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
            elif provider.kind == "chatgpt_oauth":
                result = _call_chatgpt_oauth(provider, request, model, home)
            else:
                raise ValueError(f"Unsupported provider kind: {provider.kind}")
            # Use replace (not to_dict round-trip) so typed tool_calls survive.
            result = replace(
                result,
                fallback_used=index > 0,
                cost_usd=price_call(result.model, result.input_tokens, result.output_tokens, pricing),
            )
            return _emit_and_return(logs_dir, request, result, budget, spent_before)
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
