"""Errors were already tolerated. Hangs were not.

A handler that raises is caught at every layer — the agent loop feeds it back
as a tool result, a workflow node fails and error edges route around it, the
supervisor contains it per-run. But a handler that *blocks* had no bound
anywhere: not in `dispatch_action`, not in the agent loop, not in the tick. The
supervisor is a single sequential process, so one wedged capability stalled
every agent behind it and the next tick stacked on top, with no event to say
why.

Two bounds, at different altitudes and with honestly different strengths:

- **the tick budget** stops the supervisor starting work it has no time for.
  It cannot reclaim a run already hung, but deferred agents keep their pending
  tasks and wake next tick, so a total stall becomes visible slowness.
- **the dispatch timeout** fails the step and names the capability. It
  *abandons* the handler rather than killing it — Python cannot interrupt a
  synchronous call from outside, and pretending otherwise would be the more
  dangerous bug. Real reclamation is Milestone E's out-of-process work.

The tests below assert both the guarantee and the limitation, because a reader
who assumes the thread died would write unsafe code on top of this.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import default_capability_timeout, max_tick_seconds
from jigga.core.io import read_yaml, write_yaml
from jigga.core.models import WorkflowStep
from jigga.runtime.capabilities import CapabilityManifest
from jigga.runtime.dispatcher import (
    CapabilityTimeout,
    _run_with_timeout,
    capability_timeout,
)


def _capability(**overrides) -> CapabilityManifest:
    data = {"name": "slow", "version": "1.0.0", "summary": "A capability for testing.",
            "actions": ["slow.do"], "type": "native"}
    data.update(overrides)
    return CapabilityManifest.from_dict(data)


def _step() -> WorkflowStep:
    return WorkflowStep(id="s1", action="slow.do", input={})


def _run(call, seconds: float, logs: Path):
    return _run_with_timeout(call, seconds=seconds, capability=_capability(), step=_step(),
                             logs_dir=logs, workflow_id="wf", run_id="run")


# --- the dispatch-level bound -----------------------------------------------


def test_a_fast_handler_returns_its_value_untouched(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    assert _run(lambda: {"ok": True}, 5.0, paths.logs) == {"ok": True}


def test_a_handler_that_raises_still_raises_on_the_callers_thread(tmp_path: Path) -> None:
    """The existing tolerance must survive: callers catch handler exceptions and
    feed them back to the model. A timeout wrapper that swallowed or re-wrapped
    them would break that."""
    paths = init_runtime(tmp_path)

    def _boom():
        raise ValueError("handler said no")

    with pytest.raises(ValueError, match="handler said no"):
        _run(_boom, 5.0, paths.logs)


def test_a_hung_handler_times_out_and_names_the_capability(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    release = threading.Event()

    with pytest.raises(CapabilityTimeout) as caught:
        _run(lambda: release.wait(30), 0.05, paths.logs)

    release.set()  # let the abandoned worker finish so the suite doesn't leak it
    assert caught.value.capability == "slow"
    assert caught.value.action == "slow.do"
    assert "0.05s" in str(caught.value)


def test_the_timeout_is_recorded_in_the_audit_log(tmp_path: Path) -> None:
    """A hang has to leave a trace: before this it produced no event at all."""
    import json

    paths = init_runtime(tmp_path)
    release = threading.Event()
    with pytest.raises(CapabilityTimeout):
        _run(lambda: release.wait(30), 0.05, paths.logs)
    release.set()

    rows = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines() if line.strip()]
    timeouts = [r for r in rows if r["type"] == "capability.invocation.timeout"]
    assert len(timeouts) == 1
    details = timeouts[0]["details"]
    assert details["capability"] == "slow" and details["action"] == "slow.do"
    assert details["timeout_seconds"] == 0.05
    assert "not killed" in details["note"]  # the limitation is on the record, not just in a docstring


def test_the_abandoned_handler_keeps_running(tmp_path: Path) -> None:
    """The limitation, asserted rather than described.

    Anyone building on this must not assume the work stopped — it did not. A
    handler that acquired a lock or opened a transaction still holds it.
    """
    paths = init_runtime(tmp_path)
    release = threading.Event()
    finished = threading.Event()

    def _slow():
        release.wait(30)
        finished.set()
        return "done anyway"

    with pytest.raises(CapabilityTimeout):
        _run(_slow, 0.05, paths.logs)

    assert not finished.is_set()      # it had not finished when we gave up
    release.set()
    assert finished.wait(5), "the abandoned handler should still complete — it was never killed"


def test_zero_disables_the_bound(tmp_path: Path) -> None:
    """An explicit 0 means unbounded, and must not spawn a watchdog thread."""
    paths = init_runtime(tmp_path)
    before = threading.active_count()
    assert _run(lambda: "straight through", 0, paths.logs) == "straight through"
    assert threading.active_count() == before


# --- where the number comes from --------------------------------------------


def test_a_capability_can_declare_its_own_ceiling(tmp_path: Path) -> None:
    """Something that waits on a render or a human legitimately needs longer
    than the global default."""
    capability = _capability(permissions={"limits": {"timeout_seconds": 900}})
    assert capability_timeout(capability, tmp_path) == 900


def test_the_global_default_applies_when_a_capability_is_silent(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    assert capability_timeout(_capability(), tmp_path) == default_capability_timeout(tmp_path)


def test_the_global_default_is_configurable(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    config = tmp_path / "config.yaml"
    data = read_yaml(config)
    data["capabilities"] = {"default_timeout_seconds": 7}
    write_yaml(config, data)
    assert default_capability_timeout(tmp_path) == 7
    assert capability_timeout(_capability(), tmp_path) == 7


def test_a_nonsense_declared_timeout_falls_back_rather_than_crashing(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    capability = _capability(permissions={"limits": {"timeout_seconds": "soon"}})
    assert capability_timeout(capability, tmp_path) == default_capability_timeout(tmp_path)


def test_a_negative_timeout_is_clamped_not_honoured(tmp_path: Path) -> None:
    """A negative ceiling would otherwise time out instantly, breaking every
    call rather than bounding it."""
    capability = _capability(permissions={"limits": {"timeout_seconds": -5}})
    assert capability_timeout(capability, tmp_path) == 0


# --- the tick budget ---------------------------------------------------------


def test_the_tick_budget_has_a_default_and_is_configurable(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    assert max_tick_seconds(tmp_path) == 300
    config = tmp_path / "config.yaml"
    data = read_yaml(config)
    data["supervisor"] = {"max_tick_seconds": 30}
    write_yaml(config, data)
    assert max_tick_seconds(tmp_path) == 30


def test_a_tick_defers_agents_once_the_budget_is_spent(tmp_path: Path, monkeypatch) -> None:
    """A slow agent must not consume the whole tick silently: the rest are
    deferred (keeping their pending tasks) and the exhaustion is logged."""
    import json

    from jigga.runtime import supervisor
    from jigga.runtime.tasks import create_task

    paths = init_runtime(tmp_path)
    for name in ("alpha", "beta", "gamma"):
        write_yaml(paths.agents / f"{name}.yaml", {"id": name, "name": name, "role": "r", "tools": []})
        create_task(paths.tasks, title=f"work for {name}", assignee=name)

    config = tmp_path / "config.yaml"
    data = read_yaml(config)
    data["supervisor"] = {"max_tick_seconds": 0.01}
    write_yaml(config, data)

    # Each agent "takes" longer than the whole budget, so only the first runs.
    def _slow_agent(*_a, **_k):
        time.sleep(0.05)
        return {"agent": "ran"}

    monkeypatch.setattr(supervisor, "run_agent", _slow_agent)
    result = supervisor.supervisor_tick(tmp_path)

    assert len(result["runs"]) == 1, "the budget should stop the tick after the first slow agent"
    assert len(result["deferred"]) == 2
    rows = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines() if line.strip()]
    exhausted = [r for r in rows if r["type"] == "supervisor.tick_budget_exhausted"]
    assert exhausted and exhausted[-1]["details"]["deferred"] == result["deferred"]


def test_deferred_agents_keep_their_pending_tasks(tmp_path: Path, monkeypatch) -> None:
    """Deferral must not lose work — that would trade a stall for silent data
    loss, which is worse."""
    from jigga.runtime import supervisor
    from jigga.runtime.tasks import create_task, list_tasks

    paths = init_runtime(tmp_path)
    for name in ("alpha", "beta"):
        write_yaml(paths.agents / f"{name}.yaml", {"id": name, "name": name, "role": "r", "tools": []})
        create_task(paths.tasks, title=f"work for {name}", assignee=name)
    config = tmp_path / "config.yaml"
    data = read_yaml(config)
    data["supervisor"] = {"max_tick_seconds": 0.01}
    write_yaml(config, data)
    monkeypatch.setattr(supervisor, "run_agent", lambda *a, **k: time.sleep(0.05) or {"agent": "ran"})

    result = supervisor.supervisor_tick(tmp_path)

    deferred = set(result["deferred"])
    still_pending = {t.assignee for t in list_tasks(paths.tasks) if t.state == "pending"}
    assert deferred and deferred <= still_pending


def test_a_zero_budget_means_unbounded(tmp_path: Path, monkeypatch) -> None:
    """Opting out has to be possible — someone with one long-running agent and
    no others should not be forced into deferral."""
    from jigga.runtime import supervisor
    from jigga.runtime.tasks import create_task

    paths = init_runtime(tmp_path)
    for name in ("alpha", "beta"):
        write_yaml(paths.agents / f"{name}.yaml", {"id": name, "name": name, "role": "r", "tools": []})
        create_task(paths.tasks, title=f"work for {name}", assignee=name)
    config = tmp_path / "config.yaml"
    data = read_yaml(config)
    data["supervisor"] = {"max_tick_seconds": 0}
    write_yaml(config, data)
    monkeypatch.setattr(supervisor, "run_agent", lambda *a, **k: time.sleep(0.02) or {"agent": "ran"})

    result = supervisor.supervisor_tick(tmp_path)

    assert result["deferred"] == []
    assert len(result["runs"]) == 2


# --- the thread boundary must be invisible ----------------------------------


def test_context_variables_survive_the_move_off_thread(tmp_path: Path) -> None:
    """Moving the handler onto a worker thread nearly broke two things silently.

    The secret broker's binding, the trace id and the actor all live in
    contextvars, and contextvars do NOT cross a thread boundary on their own. A
    bare `Thread(target=call)` runs the handler with no bound secrets and files
    its audit events unattributed — which is how this was caught: a test that
    expected a bound secret instead fell through to a real network call.
    """
    import contextvars

    paths = init_runtime(tmp_path)
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")
    marker.set("set-by-caller")

    seen = _run(lambda: marker.get(), 5.0, paths.logs)

    assert seen == "set-by-caller", "the handler ran outside the caller's context"


def test_the_trace_id_still_reaches_a_handler_run_under_timeout(tmp_path: Path) -> None:
    """The concrete consequence: audit events written by the handler must stay
    attached to the same trace as the step that invoked it."""
    from jigga.runtime.audit import current_trace_id, trace_context

    paths = init_runtime(tmp_path)
    with trace_context():
        expected = current_trace_id()
        assert _run(lambda: current_trace_id(), 5.0, paths.logs) == expected
