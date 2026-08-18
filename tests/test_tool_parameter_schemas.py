"""Actions can declare what they take.

Every tool was advertised to the model as `{"properties": {}}` — "takes
anything" — so it had to infer from a one-line summary that read_file wants
`path` and search_files wants `pattern`. When it guessed wrong it probed again,
and each probe is a whole model round trip. chief's 45-second replies were
seven sequential calls.

Declaring the shape is additive: a capability that declares nothing keeps the
open object, because the handler validates either way and an empty schema is
at least honest.
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.runtime.agent import _build_tool_schemas, _parameters_for, _summarize_tool_input
from jigga.runtime.capabilities import CapabilityManifest, CapabilityRegistry


def _manifest(**overrides) -> CapabilityManifest:
    data = {"name": "demo", "version": "0.1.0", "summary": "s", "actions": ["demo.act"], **overrides}
    return CapabilityManifest.from_dict(data)


# --- the schema the model is offered -----------------------------------------


def test_an_action_with_no_declared_shape_keeps_the_open_object(tmp_path: Path) -> None:
    schema = _parameters_for("demo.act", _manifest())
    assert schema == {"type": "object", "properties": {}, "additionalProperties": True}


def test_a_declared_shape_becomes_properties_and_required(tmp_path: Path) -> None:
    capability = _manifest(action_inputs={
        "demo.act": {
            "path": {"type": "string", "required": True, "description": "What to read."},
            "limit": {"type": "integer", "description": "How many."},
        },
    })
    schema = _parameters_for("demo.act", capability)
    assert schema["properties"]["path"] == {"type": "string", "description": "What to read."}
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["required"] == ["path"]
    # Still permissive: a handler may accept more than it advertises.
    assert schema["additionalProperties"] is True


def test_required_is_omitted_when_nothing_is_required(tmp_path: Path) -> None:
    capability = _manifest(action_inputs={"demo.act": {"note": {"type": "string"}}})
    assert "required" not in _parameters_for("demo.act", capability)


def test_a_sibling_action_is_unaffected(tmp_path: Path) -> None:
    # Declaring one action's shape must not imply anything about another's.
    capability = _manifest(actions=["demo.act", "demo.other"],
                           action_inputs={"demo.act": {"path": {"type": "string"}}})
    assert _parameters_for("demo.other", capability)["properties"] == {}


def test_manifests_carry_action_inputs_through_from_dict() -> None:
    capability = _manifest(action_inputs={"demo.act": {"path": {"type": "string"}}})
    assert capability.action_inputs["demo.act"]["path"]["type"] == "string"


# --- the bundled actions an agent actually leans on ---------------------------


def test_the_filesystem_actions_declare_their_arguments(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    registry = CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities",
                                       approvals_dir=tmp_path / "policies")
    schemas = {s["function"]["name"]: s["function"]["parameters"]
               for s in _build_tool_schemas(
                   ["filesystem.read_file", "filesystem.search_files", "memory.search"], registry)}
    assert schemas["filesystem__read_file"]["required"] == ["path"]
    # The one the model could not have guessed: search takes a pattern AND a path.
    assert set(schemas["filesystem__search_files"]["required"]) == {"path", "pattern"}
    assert schemas["memory__search"]["required"] == ["query"]


def test_parameter_names_match_what_the_handler_reads(tmp_path: Path) -> None:
    """A schema that advertises the wrong field name is worse than none."""
    init_runtime(tmp_path)
    registry = CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities",
                                       approvals_dir=tmp_path / "policies")
    capability = registry.resolve_action("filesystem.write_file")
    declared = set(capability.action_inputs["filesystem.write_file"])
    source = Path("jigga/runtime/filesystem.py").read_text(encoding="utf-8")
    for name in declared - {"path"}:          # `path` goes through _require_path
        assert f'resolved_input.get("{name}"' in source, f"handler never reads {name!r}"


# --- what the model asked for is now in the audit -----------------------------


def test_tool_call_input_is_summarized_for_the_log() -> None:
    assert _summarize_tool_input({"path": "/tmp/x"}) == '{"path": "/tmp/x"}'


def test_a_huge_argument_is_truncated_not_dumped() -> None:
    # A write_file call carries a whole file; the audit log is not the place
    # for it, but "there was an argument" still needs to be visible.
    out = _summarize_tool_input({"content": "x" * 5000})
    assert len(out) < 400 and out.endswith("chars)")


def test_unserializable_arguments_do_not_break_the_run() -> None:
    assert _summarize_tool_input(object()) != ""


def test_the_audit_event_records_the_arguments(tmp_path: Path, capsys) -> None:
    from jigga.core.io import write_yaml
    from jigga.runtime.audit import append_event

    init_runtime(tmp_path)
    write_yaml(tmp_path / "agents" / "a.yaml", {"id": "a", "name": "A", "role": "r"})
    append_event(tmp_path / "logs", "agent.tool_call.requested", agent="a",
                 action="filesystem.read_file", input=_summarize_tool_input({"path": "/tmp/x"}))
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "audit", "--type", "agent.tool_call.requested",
                 "--json"]) == 0
    events = json.loads(capsys.readouterr().out)
    assert events[0]["details"]["input"] == '{"path": "/tmp/x"}'
