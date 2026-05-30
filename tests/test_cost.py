from __future__ import annotations

import json
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.cli import main
from jigga.runtime.audit import append_event
from jigga.runtime.cost import (
    agent_spend,
    budget_status,
    cost_summary,
    estimate_tokens,
    price_call,
)
from jigga.runtime.model_router import ModelCallItem, ModelCallRequest, call_model


def _events(logs: Path) -> list[dict]:
    path = logs / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _set_pricing(paths, pricing: dict) -> None:
    config = read_yaml(paths.config)
    config["models"]["pricing"] = pricing
    write_yaml(paths.config, config)


def _set_budgets(paths, budgets: dict) -> None:
    config = read_yaml(paths.config)
    config["budgets"] = budgets
    write_yaml(paths.config, config)


def _request(agent_id: str = "agent_x", text: str = "hello there general kenobi") -> ModelCallRequest:
    return ModelCallRequest(
        agent_id=agent_id, role="r", task={"id": "t1", "title": "do"},
        items=[ModelCallItem(id="system", role="system", content=text)], dry_run=True,
    )


def _seed_call(logs: Path, agent_id: str, cost: float, *, in_tok: int = 10, out_tok: int = 5) -> None:
    append_event(logs, "model.call", agent_id=agent_id, provider="p", model="m",
                 dry_run=True, cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok)


# --- pricing math ----------------------------------------------------------


def test_estimate_tokens_roughly_four_chars_each() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("a" * 40) == 10


def test_price_call_model_rate_then_default_then_free() -> None:
    pricing = {"gpt-4o": {"input_per_1k": 0.005, "output_per_1k": 0.015}, "default": {"input_per_1k": 1.0, "output_per_1k": 2.0}}
    assert price_call("gpt-4o", 1000, 1000, pricing) == 0.02
    # unknown model falls back to default rate
    assert price_call("mystery", 1000, 1000, pricing) == 3.0
    # no pricing at all → free
    assert price_call("anything", 1000, 1000, {}) == 0.0


# --- cost recorded on the model.call event ---------------------------------


def test_model_call_records_tokens_and_cost(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_pricing(paths, {"default": {"input_per_1k": 1.0, "output_per_1k": 1.0}})
    call_model(paths.home, paths.logs, _request())
    event = next(e for e in _events(paths.logs) if e["type"] == "model.call")
    details = event["details"]
    assert details["input_tokens"] > 0
    assert details["cost_usd"] > 0
    assert details["cost_usd"] == round((details["input_tokens"] + details["output_tokens"]) / 1000, 6)


# --- rollups ---------------------------------------------------------------


def test_cost_summary_rolls_up_per_agent(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    _seed_call(logs, "alpha", 0.10)
    _seed_call(logs, "alpha", 0.20)
    _seed_call(logs, "beta", 1.00)
    summary = cost_summary(logs)
    # sorted by cost desc → beta first
    assert [r["agent"] for r in summary["agents"]] == ["beta", "alpha"]
    alpha = next(r for r in summary["agents"] if r["agent"] == "alpha")
    assert alpha["calls"] == 2
    assert alpha["cost_usd"] == 0.30
    assert summary["total"]["cost_usd"] == 1.30


def test_agent_spend_filters_by_agent(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    _seed_call(logs, "alpha", 0.40)
    _seed_call(logs, "beta", 9.00)
    assert agent_spend(logs, "alpha") == 0.40


# --- budget status ---------------------------------------------------------


def test_budget_status_ok_warn_exceeded(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_budgets(paths, {"window": "all", "default": {"limit_usd": 1.0}})
    logs = paths.logs

    assert budget_status(paths.home, logs, "alpha")["state"] == "ok"
    _seed_call(logs, "alpha", 0.50)
    assert budget_status(paths.home, logs, "alpha")["state"] == "ok"
    _seed_call(logs, "alpha", 0.35)  # 0.85 → warn (>= 80%)
    status = budget_status(paths.home, logs, "alpha")
    assert status["state"] == "warn"
    assert status["remaining_usd"] == 0.15
    _seed_call(logs, "alpha", 0.20)  # 1.05 → exceeded
    assert budget_status(paths.home, logs, "alpha")["state"] == "exceeded"


def test_no_budget_returns_none(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    assert budget_status(paths.home, paths.logs, "whoever") is None


# --- enforcement in call_model ---------------------------------------------


def test_budget_hard_stop_denies_once_cap_is_spent(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_pricing(paths, {"default": {"input_per_1k": 100.0, "output_per_1k": 100.0}})
    _set_budgets(paths, {"window": "all", "default": {"limit_usd": 0.50}})

    first = call_model(paths.home, paths.logs, _request("spender"))
    assert first.status == "ok"
    assert first.cost_usd > 0.50  # one call already blows the tiny cap

    second = call_model(paths.home, paths.logs, _request("spender"))
    assert second.status == "error"
    assert "budget_exceeded" in (second.error or "")
    denied = [e for e in _events(paths.logs) if e["type"] == "budget.exceeded"]
    assert denied and denied[-1]["status"] == "deny"


def test_budget_warning_emitted_on_threshold_crossing(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    # Tuned so one call lands between 80% and 100% of the cap.
    _set_pricing(paths, {"default": {"input_per_1k": 100.0, "output_per_1k": 0.0}})
    # request text ~ a handful of tokens; size the cap off the actual cost.
    result = call_model(paths.home, paths.logs, _request("warner"))
    cost = result.cost_usd
    # reset log + set a cap where this same call crosses 80% but not 100%.
    (paths.logs / "events.jsonl").unlink()
    _set_budgets(paths, {"window": "all", "default": {"limit_usd": round(cost / 0.9, 6)}})

    call_model(paths.home, paths.logs, _request("warner"))
    warnings = [e for e in _events(paths.logs) if e["type"] == "budget.warning"]
    assert warnings and warnings[-1]["status"] == "ask"
    assert warnings[-1]["details"]["fraction"] >= 0.8


# --- CLI -------------------------------------------------------------------


def test_cli_cost_json(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    _set_budgets(paths, {"window": "all", "default": {"limit_usd": 5.0}})
    _seed_call(paths.logs, "alpha", 1.25)
    assert main(["--home", str(tmp_path), "cost", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"]["cost_usd"] == 1.25
    assert payload["budgets"]["alpha"]["state"] == "ok"
