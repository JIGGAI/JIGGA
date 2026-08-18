"""Skills as a top-level feature: trigger-driven context injection over
skill_pack capabilities, plus the `jigga skills` CLI."""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.models import AgentConfig
from jigga.runtime.capabilities import CapabilityRegistry, load_capability_manifest, record_approval
from jigga.runtime.context_pack import assemble_agent_context
from jigga.runtime.skills import (
    activated_skills_layer,
    create_skill,
    granted_skills,
    skills_summary_layer,
    trigger_matches,
)


def _install_skill(paths, name: str = "release-notes", triggers=("release notes", "changelog")) -> None:
    pack = paths.capabilities / name
    pack.mkdir(parents=True)
    (pack / "manifest.yaml").write_text(
        f"""name: {name}
version: 0.1.0
type: skill_pack
summary: Draft release notes from merged PRs.
actions:
  - {name.replace('-', '_')}.run
triggers: [{', '.join(repr(t) for t in triggers)}]
risk_level: low
instructions: instructions.md
""", encoding="utf-8")
    (pack / "instructions.md").write_text("Group PRs by theme; lead with user impact.", encoding="utf-8")
    record_approval(paths.policies, load_capability_manifest(pack / "manifest.yaml"))


def _agent(tools: list[str]) -> AgentConfig:
    return AgentConfig(id="writer", name="Writer", role="writes things", tools=tools)


def _registry(paths) -> CapabilityRegistry:
    return CapabilityRegistry.load(user_capabilities=paths.capabilities, approvals_dir=paths.policies)


def test_trigger_matching_is_whole_word() -> None:
    from jigga.runtime.capabilities import CapabilityManifest

    skill = CapabilityManifest(name="s", version="1", summary="", actions=["s.run"],
                               type="skill_pack", triggers=["mail", "release notes"])
    assert trigger_matches(skill, "Check my MAIL please")
    assert trigger_matches(skill, "draft the release notes for v2")
    assert not trigger_matches(skill, "clean the mailbox")  # substring must not fire


def test_granted_skills_respects_agent_tools(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _install_skill(paths)
    registry = _registry(paths)
    assert granted_skills(registry, _agent(["release_notes.run"]))
    assert not granted_skills(registry, _agent(["notifications.send"]))
    assert skills_summary_layer(registry, _agent(["notifications.send"])) is None


def test_context_pack_injects_summary_and_trigger_matched_instructions(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _install_skill(paths)
    registry = _registry(paths)
    agent = _agent(["release_notes.run"])

    text, layers, _ = assemble_agent_context(
        paths.home, agent, "writer", registry=registry, task_text="please draft the changelog for v2")
    assert "skills" in layers and "skills-active" in layers
    assert "release-notes — Draft release notes" in text
    assert "Group PRs by theme" in text

    # No trigger match → summary only, instructions stay out of context.
    text, layers, _ = assemble_agent_context(
        paths.home, agent, "writer", registry=registry, task_text="water the plants")
    assert "skills" in layers and "skills-active" not in layers
    assert "Group PRs by theme" not in text


def test_activated_layer_requires_task_text(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _install_skill(paths)
    assert activated_skills_layer(_registry(paths), _agent(["release_notes.run"]), None) is None


def test_create_skill_scaffolds_and_requires_approval(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    result = create_skill(paths.capabilities, "Meeting Prep!")
    assert result["name"] == "meeting-prep"
    pack = Path(result["dir"])
    assert (pack / "manifest.yaml").exists() and (pack / "instructions.md").exists()
    # Unapproved → pending, not active (creation never auto-trusts).
    registry = _registry(paths)
    assert "meeting-prep" in [c.name for c in registry.list_pending()]
    assert not [s for s in granted_skills(registry, _agent(["meeting_prep.run"]))]


def test_cli_skills_list_show_create(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "init", "--examples"]) == 0
    paths = init_runtime(tmp_path, examples=True)
    _install_skill(paths)
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "skills", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [s["name"] for s in data["skills"]] == ["release-notes"]
    assert data["skills"][0]["triggers"] == ["release notes", "changelog"]
    assert main(["--home", str(tmp_path), "skills", "show", "release-notes"]) == 0
    assert "Group PRs by theme" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "skills", "create", "travel-briefs"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "travel-briefs" and "capabilities approve" in out["next"]
