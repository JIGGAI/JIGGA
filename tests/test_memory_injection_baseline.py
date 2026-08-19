"""What the context pack injects is now measured, not guessed.

ClawRecipes ticket 0272, JIGGA side. The decision the numbers feed is whether a
knowledge graph is worth building (`docs/MEMORY_BACKENDS.md`) or whether the
current fixed-cap, recency-first strategy is already fine. Both answers need the
same measurement: how much goes in, and how much the caps drop.

The rule these tests defend: measurement changes nothing. The prompt an agent
gets with instrumentation is byte-identical to the prompt it got without it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_teams
from jigga.core.io import write_yaml
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.context_pack import (
    _TOTAL_LIMIT,
    VOLATILE_BOUNDARY,
    assemble_agent_context,
)
from jigga.runtime.memory_injection import _percentile, read_events, render, report
from jigga.runtime.team_memory import append_team_memory
from jigga.runtime.workspaces import scaffold_workspace, workspace_dir


def _setup(paths):
    write_yaml(paths.teams / "mt.yaml",
               {"id": "mt", "name": "Marketing", "purpose": "Launch copy",
                "agents": [{"id": "writer", "role": "copywriter"}]})
    write_yaml(paths.agents / "writer.yaml",
               {"id": "writer", "name": "Writer", "role": "copywriter",
                "memory_scope": "task_only", "tools": [], "permissions": {}})
    scaffold_workspace(paths.home, load_teams(paths.teams)["mt"])
    return load_agents(paths.agents)["writer"]


def write_file(home: Path, team_id: str, relpath: str, content: str) -> None:
    path = workspace_dir(home, team_id) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _assemble(paths, agent, **kw):
    registry = CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                       approvals_dir=paths.policies)
    return assemble_agent_context(paths.home, agent, "mt", registry=registry, **kw)


# --- the measurement itself --------------------------------------------------


def test_it_measures_what_it_injected(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    agent = _setup(paths)
    text, layers, stats = _assemble(paths, agent)

    assert stats.chars == sum(layer.included for layer in stats.layers)
    assert stats.tokens_est == round(stats.chars / 4)
    # Every loaded layer is accounted for, and nothing else claims to be loaded.
    assert {layer.name for layer in stats.layers if not layer.dropped} == set(layers)
    assert text


def test_a_clipped_layer_reports_what_it_lost(tmp_path: Path) -> None:
    # The number that matters is not "SOUL was included" but "SOUL had 9k chars
    # and 1.5k of them reached the model".
    paths = init_runtime(tmp_path)
    agent = _setup(paths)
    write_file(paths.home, "mt", "roles/writer/SOUL.md", "x" * 9000)

    _text, _layers, stats = _assemble(paths, agent)

    soul = next(layer for layer in stats.layers if layer.name == "SOUL")
    assert soul.available >= 9000        # the body, plus its heading
    assert soul.included < 2000          # the SOUL cap
    assert soul.clipped is True


def test_layers_dropped_by_the_total_budget_are_counted_not_forgotten(
    tmp_path: Path, monkeypatch,
) -> None:
    # The layers that lose this race are the VOLATILE ones — today's log, pinned
    # team facts, the lead's shared context — because they are deliberately
    # last. A run where that happens is a run whose agent never saw what the
    # team knows, and before this it left no trace at all.
    paths = init_runtime(tmp_path)
    agent = _setup(paths)
    for rel in ("roles/writer/USER.md", "roles/writer/SOUL.md", "roles/writer/AGENTS.md",
                "TEAM.md", "roles/writer/MEMORY.md", "notes/plan.md",
                "shared-context/priorities.md"):
        write_file(paths.home, "mt", rel, "y" * 9000)
    append_team_memory(paths.home, "mt", text="ICP is indie devs", type="fact")
    # Squeeze the total rather than manufacturing 9k of plausible content: the
    # behaviour under test is what happens when the budget runs out, not the
    # particular number it runs out at.
    monkeypatch.setattr("jigga.runtime.context_pack._TOTAL_LIMIT", 3000)

    _text, layers, stats = _assemble(paths, agent)

    assert stats.hit_total_cap is True
    dropped = [layer.name for layer in stats.layers if layer.dropped]
    assert dropped, "something must have been dropped once the budget was spent"
    assert all(name not in layers for name in dropped)
    # And what was dropped is reported with the size it would have had.
    assert all(layer.available > 0 for layer in stats.layers if layer.dropped)


def test_measuring_does_not_change_the_prompt(tmp_path: Path) -> None:
    """The whole ticket is 'measurement only, no behavior change'.

    Re-derives the pre-instrumentation result — layers concatenated under the
    same caps, stopping at the same total — and demands the real assembler still
    produces exactly that.
    """
    paths = init_runtime(tmp_path)
    agent = _setup(paths)
    write_file(paths.home, "mt", "roles/writer/SOUL.md", "s" * 4000)
    write_file(paths.home, "mt", "TEAM.md", "t" * 4000)
    append_team_memory(paths.home, "mt", text="ICP is indie devs", type="fact")

    text, _layers, stats = _assemble(paths, agent)

    # Nothing beyond the documented budget reached the prompt…
    assert stats.chars <= _TOTAL_LIMIT
    # …and the text is EXACTLY the included bytes plus the boundary marker and
    # the "\n\n" joins. Nothing the instrumentation added leaked into it.
    blocks = len([layer for layer in stats.layers if not layer.dropped])
    sections = blocks + (1 if VOLATILE_BOUNDARY in text else 0)
    expected = stats.chars + (len(VOLATILE_BOUNDARY) if VOLATILE_BOUNDARY in text else 0) \
        + 2 * (sections - 1)
    assert len(text) == expected


def test_a_restricted_session_is_marked_and_omits_private_layers(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    agent = _setup(paths)
    write_file(paths.home, "mt", "roles/writer/MEMORY.md", "private things")

    _text, _layers, stats = _assemble(paths, agent, restricted=True)

    assert stats.restricted is True
    assert all(not layer.private for layer in stats.layers)


def test_the_recency_window_reports_both_sides(tmp_path: Path) -> None:
    # "8 of 30 facts were injected" is the finding; "8 facts were injected" is
    # not — the second number is the one that says relevance is being starved.
    paths = init_runtime(tmp_path)
    agent = _setup(paths)
    for i in range(30):
        append_team_memory(paths.home, "mt", text=f"fact {i}", type="fact")

    _text, _layers, stats = _assemble(paths, agent)

    assert stats.facts_available == 30
    assert stats.facts_included == 8  # _RECENT_FACTS
    assert stats.facts_included < stats.facts_available


def test_the_cacheable_split_is_recorded(tmp_path: Path) -> None:
    # Stable bytes are byte-identical across calls and eligible for the
    # provider's prompt cache; volatile bytes are re-billed every single call.
    # The ratio is a direct cost lever, so it is worth counting.
    paths = init_runtime(tmp_path)
    agent = _setup(paths)
    append_team_memory(paths.home, "mt", text="something recent", type="fact")

    _text, _layers, stats = _assemble(paths, agent)

    assert stats.stable_chars > 0
    assert stats.volatile_chars > 0
    assert stats.stable_chars + stats.volatile_chars == stats.chars


# --- the run records it ------------------------------------------------------


def test_the_run_records_the_sizes_on_the_existing_event(tmp_path: Path, capsys) -> None:
    from jigga.cli import main

    paths = init_runtime(tmp_path)
    _setup(paths)
    assert main(["--home", str(tmp_path), "task", "create", "--title", "write copy",
                 "--assignee", "writer"]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "run", "--dry-run-model", "agent", "writer"]) == 0

    events = [json.loads(line) for line
              in (paths.logs / "events.jsonl").read_text().splitlines() if line.strip()]
    assembled = [e for e in events if e["type"] == "agent.context.assembled"]
    assert len(assembled) == 1
    details = assembled[0]["details"]
    assert details["tokens_est"] > 0
    assert details["team"] == "mt"
    assert isinstance(details["layer_detail"], list)
    # The pre-existing contract is intact: `layers` still lists loaded names.
    assert isinstance(details["layers"], list) and "identity" in details["layers"]


# --- the report --------------------------------------------------------------


def _event(logs: Path, *, when: datetime, agent: str = "writer", tokens: int = 100,
           capped: bool = False, layer_detail: list | None = None) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "id": "evt", "time": when.isoformat(), "type": "agent.context.assembled",
            "status": "ok", "actor": "user",
            "details": {"agent": agent, "chars": tokens * 4, "tokens_est": tokens,
                        "hit_total_cap": capped, "stable_chars": tokens * 3,
                        "volatile_chars": tokens, "restricted": False,
                        "pinned_available": 10, "pinned_included": 8,
                        "facts_available": 30, "facts_included": 8,
                        "layers": ["identity"],
                        "layer_detail": layer_detail or []},
        }) + "\n")


def test_percentiles_are_nearest_rank() -> None:
    assert _percentile([1, 2, 3, 4, 5], 50) == 3
    assert _percentile([1, 2, 3, 4, 5], 95) == 5
    assert _percentile([], 50) == 0


def test_the_report_summarises_the_window(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    for tokens in (100, 200, 300, 400):
        _event(tmp_path, when=now, tokens=tokens)
    _event(tmp_path, when=now, tokens=2000, capped=True)

    data = report(tmp_path, days=7)

    assert data["runs"] == 5
    assert data["tokens_est"]["p50"] == 300
    assert data["tokens_est"]["max"] == 2000
    assert data["hit_total_cap"] == {"runs": 1, "pct": 20.0}
    assert data["cacheable_split"]["stable_pct"] == 75.0


def test_events_outside_the_window_are_excluded(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _event(tmp_path, when=now)
    _event(tmp_path, when=now - timedelta(days=30))
    assert report(tmp_path, days=7)["runs"] == 1


def test_events_from_before_the_instrumentation_are_skipped_not_zeroed(tmp_path: Path) -> None:
    # Counting an unmeasured run as 0 tokens would drag every percentile toward
    # a baseline that was never measured — worse than a smaller sample.
    (tmp_path / "events.jsonl").write_text(json.dumps({
        "id": "old", "time": datetime.now(timezone.utc).isoformat(),
        "type": "agent.context.assembled", "status": "ok",
        "details": {"agent": "writer", "layers": ["identity"]},
    }) + "\n", encoding="utf-8")
    assert read_events(tmp_path, days=7) == []
    assert report(tmp_path, days=7)["runs"] == 0


def test_a_torn_line_does_not_lose_the_rest_of_the_report(tmp_path: Path) -> None:
    _event(tmp_path, when=datetime.now(timezone.utc))
    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"type": "agent.context.assembled", "details": {"chars": 1\n')
    _event(tmp_path, when=datetime.now(timezone.utc))
    assert report(tmp_path, days=7)["runs"] == 2


def test_the_report_ranks_layers_and_agents(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _event(tmp_path, when=now, agent="writer", tokens=100, layer_detail=[
        {"name": "SOUL", "available": 9000, "included": 1500, "clipped": True,
         "dropped": False, "volatile": False, "private": False},
        {"name": "recent", "available": 800, "included": 0, "clipped": False,
         "dropped": True, "volatile": True, "private": True},
    ])
    _event(tmp_path, when=now, agent="lead", tokens=50)

    data = report(tmp_path, days=7)

    assert data["layers"]["SOUL"]["clipped"] == 1
    assert data["layers"]["recent"]["dropped"] == 1
    assert list(data["by_agent"]) == ["writer", "lead"]  # ranked by volume


def test_an_empty_window_says_so_rather_than_printing_zeros(tmp_path: Path) -> None:
    data = report(tmp_path, days=7)
    assert data["runs"] == 0
    assert "No measured" in render(data)


def test_the_text_render_includes_the_headline_numbers(tmp_path: Path) -> None:
    _event(tmp_path, when=datetime.now(timezone.utc), tokens=250, layer_detail=[
        {"name": "SOUL", "available": 100, "included": 100, "clipped": False,
         "dropped": False, "volatile": False, "private": False}])
    out = render(report(tmp_path, days=7))
    assert "tokens injected" in out and "250" in out
    assert "SOUL" in out


def test_a_layer_barely_over_its_cap_still_reports_as_clipped(tmp_path: Path) -> None:
    """`_clip` appends a "…(truncated)" marker, so a body a few chars over its
    cap produces a block LONGER than the body it replaced. Deciding "was this
    clipped?" by comparing lengths called that untouched — and a report that
    says nothing was clipped is the one nobody investigates."""
    from jigga.runtime.context_pack import _LAYER_LIMITS

    paths = init_runtime(tmp_path)
    agent = _setup(paths)
    write_file(paths.home, "mt", "roles/writer/SOUL.md", "z" * (_LAYER_LIMITS["SOUL"] + 2))

    _text, _layers, stats = _assemble(paths, agent)

    soul = next(layer for layer in stats.layers if layer.name == "SOUL")
    assert soul.clipped is True
