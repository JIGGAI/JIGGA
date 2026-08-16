"""A durable inbox for events pushed from outside the heartbeat.

The design follows entirely from refusing to execute in the receiving path. A
webhook listener writes a file and returns; the supervisor runs it on the next
tick. That buys a fast HTTP response (providers time out and retry), crash
safety (on disk before the sender is told OK), bounded concurrency (execution
stays inside the #189 tick budget), and one execution path shared with pull
triggers instead of a second one that drifts.

Three properties are load-bearing:

**Dedup, because delivery is at-least-once.** Every webhook provider retries.
Without an idempotency key a retry is a second run.

**Claim before executing, never auto-retry.** A drained event moves to
`processing/` *before* it runs, so a crash leaves a visible claimed entry
rather than an event that silently replays. A half-executed side effect must
not be blindly repeated — the same contract `recovery.py` holds for tasks.

**Opt-in targeting.** The queue is reachable from outside. If a sender could
name any workflow, the webhook endpoint would be remote arbitrary execution
against the agent runtime. A pushed event runs only a workflow that declared a
matching `webhook:` trigger.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime import event_queue
from jigga.runtime.event_queue import (
    QueueFull,
    claim,
    enqueue,
    fail,
    list_failed,
    pending_count,
    stats,
    sweep_stale_processing,
)

NOW = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)


def _payload(**extra) -> dict:
    return {"workflow": "publish_result", **extra}


# --- accepting ----------------------------------------------------------------


def test_an_event_is_durable_before_the_sender_is_told_ok(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)

    result = enqueue(paths, source="postiz", kind="publish_result", payload=_payload(id="p1"))

    assert result["status"] == "accepted"
    assert pending_count(paths) == 1


def test_events_drain_in_arrival_order(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    for index in range(3):
        enqueue(paths, source="postiz", kind="publish_result",
                payload=_payload(n=index), now=NOW + timedelta(seconds=index))

    order = [record["payload"]["n"] for _path, record in claim(paths, 10)]

    assert order == [0, 1, 2]


# --- dedup --------------------------------------------------------------------


def test_a_retried_delivery_does_not_run_twice(tmp_path: Path) -> None:
    """Every webhook provider retries — some aggressively on a slow response."""
    paths = init_runtime(tmp_path)

    first = enqueue(paths, source="postiz", kind="publish_result",
                    payload=_payload(), idempotency_key="delivery-1")
    second = enqueue(paths, source="postiz", kind="publish_result",
                     payload=_payload(), idempotency_key="delivery-1")

    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    assert pending_count(paths) == 1


def test_distinct_deliveries_are_both_accepted(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    enqueue(paths, source="postiz", kind="publish_result", payload=_payload(), idempotency_key="a")
    enqueue(paths, source="postiz", kind="publish_result", payload=_payload(), idempotency_key="b")
    assert pending_count(paths) == 2


def test_dedup_survives_the_event_being_drained(tmp_path: Path) -> None:
    """A provider that retries *after* the run completed must still not re-fire
    — the key is remembered, not just the pending file."""
    paths = init_runtime(tmp_path)
    enqueue(paths, source="postiz", kind="publish_result", payload=_payload(), idempotency_key="k")
    for path, _record in claim(paths, 10):
        event_queue.complete(paths, path)

    assert enqueue(paths, source="postiz", kind="publish_result",
                   payload=_payload(), idempotency_key="k")["status"] == "duplicate"


def test_an_event_without_a_key_is_never_deduped(tmp_path: Path) -> None:
    """Absent a key there is no basis for calling two deliveries the same."""
    paths = init_runtime(tmp_path)
    enqueue(paths, source="cli", kind="publish_result", payload=_payload())
    enqueue(paths, source="cli", kind="publish_result", payload=_payload())
    assert pending_count(paths) == 2


# --- bounded ------------------------------------------------------------------


def test_the_queue_refuses_rather_than_growing_without_limit(tmp_path: Path) -> None:
    """A burst that outruns the drain must not fill the disk."""
    paths = init_runtime(tmp_path)
    config = tmp_path / "config.yaml"
    data = read_yaml(config)
    data["events"] = {"max_pending": 3}
    write_yaml(config, data)

    for _ in range(3):
        enqueue(paths, source="postiz", kind="publish_result", payload=_payload())
    with pytest.raises(QueueFull):
        enqueue(paths, source="postiz", kind="publish_result", payload=_payload())


def test_a_full_queue_raises_rather_than_dropping_silently(tmp_path: Path) -> None:
    """Dropping would lose the event while the sender believes it landed. The
    caller has to be able to answer with a retryable status."""
    paths = init_runtime(tmp_path)
    config = tmp_path / "config.yaml"
    data = read_yaml(config)
    data["events"] = {"max_pending": 1}
    write_yaml(config, data)
    enqueue(paths, source="postiz", kind="publish_result", payload=_payload())

    with pytest.raises(QueueFull, match="retry later"):
        enqueue(paths, source="postiz", kind="publish_result", payload=_payload())


# --- claim / crash ------------------------------------------------------------


def test_claiming_moves_the_event_out_of_pending(tmp_path: Path) -> None:
    """Claimed before it runs, so a crash cannot silently replay a side effect."""
    paths = init_runtime(tmp_path)
    enqueue(paths, source="postiz", kind="publish_result", payload=_payload())

    claimed = list(claim(paths, 10))

    assert len(claimed) == 1
    assert stats(paths) == {"pending": 0, "processing": 1, "failed": 0}


def test_a_stranded_claim_is_swept_to_failed_not_retried(tmp_path: Path) -> None:
    """The crash case. It becomes visible and a human decides — the runtime
    cannot know whether a partially-applied effect is safe to repeat."""
    paths = init_runtime(tmp_path)
    enqueue(paths, source="postiz", kind="publish_result", payload=_payload())
    list(claim(paths, 10))

    swept = sweep_stale_processing(paths, now=datetime.now(timezone.utc) + timedelta(days=1))

    assert len(swept) == 1
    assert stats(paths) == {"pending": 0, "processing": 0, "failed": 1}
    assert "side effects unknown" in list_failed(paths)[0]["error"]


def test_a_freshly_claimed_event_is_not_swept(tmp_path: Path) -> None:
    """A long workflow is not a fault."""
    paths = init_runtime(tmp_path)
    enqueue(paths, source="postiz", kind="publish_result", payload=_payload())
    list(claim(paths, 10))
    assert sweep_stale_processing(paths) == []


def test_a_failed_event_records_why(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    enqueue(paths, source="postiz", kind="publish_result", payload=_payload())
    for path, _record in claim(paths, 10):
        fail(paths, path, "handler exploded")

    failed = list_failed(paths)
    assert len(failed) == 1 and failed[0]["error"] == "handler exploded"


def test_claiming_respects_the_limit(tmp_path: Path) -> None:
    """Bounds a tick's work — the queue must not be able to monopolize it."""
    paths = init_runtime(tmp_path)
    for index in range(5):
        enqueue(paths, source="postiz", kind="publish_result",
                payload=_payload(n=index), now=NOW + timedelta(seconds=index))

    assert len(list(claim(paths, 2))) == 2
    assert pending_count(paths) == 3


# --- the security property ----------------------------------------------------


def _workflow(paths, workflow_id: str, trigger: dict) -> None:
    write_yaml(paths.workflows / f"{workflow_id}.yaml", {
        "id": workflow_id, "name": workflow_id, "trigger": trigger,
        "steps": [{"id": "s1", "agent": "worker", "action": "summarize"}]})


def _tick_drain(paths):
    from jigga.core.config import load_workflows
    from jigga.runtime.supervisor import _drain_event_queue

    return _drain_event_queue(paths, load_workflows(paths.workflows))


def test_a_pushed_event_cannot_run_a_workflow_that_did_not_opt_in(tmp_path: Path) -> None:
    """The queue is reachable from outside. If a sender could name any
    workflow, the webhook endpoint would be remote arbitrary execution."""
    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "worker.yaml",
               {"id": "worker", "name": "worker", "role": "r", "tools": []})
    _workflow(paths, "internal_only", {"schedule": "weekdays at 09:00"})
    enqueue(paths, source="attacker", kind="internal_only", payload={"workflow": "internal_only"})

    ran = _tick_drain(paths)

    assert ran == []
    failed = list_failed(paths)
    assert len(failed) == 1
    assert "does not declare a `webhook:` trigger" in failed[0]["error"]


def test_an_event_for_an_unknown_workflow_is_parked(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    enqueue(paths, source="attacker", kind="nope", payload={"workflow": "nonexistent"})

    assert _tick_drain(paths) == []
    assert "no workflow named" in list_failed(paths)[0]["error"]


def test_an_opted_in_workflow_does_run(tmp_path: Path) -> None:
    """The allow case has to actually work, or the check is just a wall."""
    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "worker.yaml",
               {"id": "worker", "name": "worker", "role": "r", "tools": ["summarize"],
                "permissions": {"memory": {"scope": "task_only"}}, "memory_scope": "task_only"})
    _workflow(paths, "publish_result", {"webhook": "publish_result"})
    enqueue(paths, source="postiz", kind="publish_result",
            payload={"workflow": "publish_result", "status": "published"})

    ran = _tick_drain(paths)

    assert len(ran) == 1
    assert stats(paths) == {"pending": 0, "processing": 0, "failed": 0}


def test_the_kind_must_match_the_declared_trigger(tmp_path: Path) -> None:
    """Opting into one event kind must not opt you into every other one."""
    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "worker.yaml",
               {"id": "worker", "name": "worker", "role": "r", "tools": []})
    _workflow(paths, "publish_result", {"webhook": "publish_result"})
    enqueue(paths, source="attacker", kind="something_else",
            payload={"workflow": "publish_result"})

    assert _tick_drain(paths) == []
    assert "does not declare a `webhook:` trigger" in list_failed(paths)[0]["error"]
