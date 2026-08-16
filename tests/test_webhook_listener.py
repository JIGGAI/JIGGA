"""JIGGA's first inbound network surface, exercised over a real socket.

The listener does two things: authenticate, and enqueue. Targeting is enforced
by the queue drain (a workflow must declare a matching `trigger.webhook`) and
execution happens on the heartbeat — so the piece exposed to the network holds
as little logic as possible.

These tests drive an actual HTTP server rather than calling the handler
directly. Auth, body limits and status codes are properties of what happens on
the wire; testing them through a Python call would prove something weaker than
what is deployed.

The ordering that matters most: **no key, no listener.** A server that silently
served anonymous requests because a secret was missing would be the worst
possible failure here, so it is asserted before anything else.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.event_queue import pending_count, stats
from jigga.runtime.secrets_broker import set_secret
from jigga.runtime.webhook import (
    SECRET_NAME,
    WebhookNotConfigured,
    build_server,
    is_enabled,
    serve_in_background,
)

KEY = "test-key-abc123"


def _configure(home: Path, **overrides) -> None:
    config = home / "config.yaml"
    data = read_yaml(config)
    data["webhook"] = {"enabled": True, "bind": "127.0.0.1", "port": 0, **overrides}
    write_yaml(config, data)


@pytest.fixture
def listener(tmp_path: Path):
    """A live server on an ephemeral port, torn down after the test."""
    paths = init_runtime(tmp_path)
    _configure(tmp_path)
    set_secret(tmp_path, SECRET_NAME, KEY)
    server = build_server(paths)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield paths, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, body: dict | bytes, *, key: str | None = KEY,
          headers: dict[str, str] | None = None) -> tuple[int, dict]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if key is not None:
        request.add_header("Authorization", f"Bearer {key}")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# --- it will not start unauthenticated ----------------------------------------


def test_no_key_means_no_listener(tmp_path: Path) -> None:
    """The single most important property. A listener that served anonymous
    requests because a secret was missing would be worse than no listener."""
    paths = init_runtime(tmp_path)
    _configure(tmp_path)

    with pytest.raises(WebhookNotConfigured, match="will not start unauthenticated"):
        build_server(paths)


def test_disabled_by_default(tmp_path: Path) -> None:
    """Opening a listening socket on someone's machine is not an implicit act."""
    init_runtime(tmp_path)
    assert is_enabled(tmp_path) is False


def test_enabled_without_a_key_reports_rather_than_starting_silently(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(tmp_path)

    assert serve_in_background(paths) is None
    events = (paths.logs / "events.jsonl").read_text()
    assert "webhook.not_started" in events


# --- authentication ------------------------------------------------------------


def test_a_request_without_a_key_is_rejected_and_enqueues_nothing(listener) -> None:
    paths, base = listener

    status, body = _post(f"{base}/hooks/publish_result", {"ok": True}, key=None)

    assert status == 401 and body["error"] == "unauthorized"
    assert pending_count(paths) == 0


def test_a_wrong_key_is_rejected(listener) -> None:
    paths, base = listener
    status, _ = _post(f"{base}/hooks/publish_result", {"ok": True}, key="not-the-key")
    assert status == 401
    assert pending_count(paths) == 0


def test_a_near_miss_key_is_not_written_to_the_audit_log(listener) -> None:
    """Recording the presented value would put someone's near-miss credential —
    quite possibly a real key with a typo — into the log."""
    paths, base = listener
    _post(f"{base}/hooks/publish_result", {"ok": True}, key="test-key-abc124")

    log = (paths.logs / "events.jsonl").read_text()
    assert "webhook.unauthorized" in log
    assert "test-key-abc124" not in log


def test_the_correct_key_is_accepted(listener) -> None:
    paths, base = listener

    status, body = _post(f"{base}/hooks/publish_result", {"status": "published"})

    assert status == 202 and body["status"] == "accepted"
    assert pending_count(paths) == 1


# --- what gets enqueued --------------------------------------------------------


def test_the_kind_comes_from_the_path(listener) -> None:
    paths, base = listener
    _post(f"{base}/hooks/publish_result", {"status": "published"})

    from jigga.runtime.event_queue import claim

    _path, record = next(iter(claim(paths, 1)))
    assert record["kind"] == "publish_result"
    assert record["payload"] == {"status": "published"}
    assert record["source"] == "webhook"


def test_a_retried_delivery_is_deduplicated(listener) -> None:
    """Providers retry; a duplicate must not become a second run."""
    paths, base = listener
    headers = {"X-Delivery-Id": "delivery-42"}

    first, _ = _post(f"{base}/hooks/publish_result", {"n": 1}, headers=headers)
    second, body = _post(f"{base}/hooks/publish_result", {"n": 1}, headers=headers)

    assert first == 202
    assert second == 200 and body["status"] == "duplicate"
    assert pending_count(paths) == 1


def test_an_identical_body_without_a_delivery_header_still_dedupes(listener) -> None:
    """Not every provider sends a delivery id, and all of them retry — the
    body hash is the fallback that keeps those deduplicable."""
    paths, base = listener
    _post(f"{base}/hooks/publish_result", {"same": "body"})
    status, _ = _post(f"{base}/hooks/publish_result", {"same": "body"})

    assert status == 200
    assert pending_count(paths) == 1


# --- hostile input --------------------------------------------------------------


def test_an_oversized_body_is_refused_before_being_read(listener) -> None:
    """An unbounded read is trivial memory exhaustion."""
    paths, base = listener
    huge = json.dumps({"blob": "x" * (128 * 1024)}).encode()

    status, body = _post(f"{base}/hooks/publish_result", huge)

    assert status == 413 and "max_bytes" in body
    assert pending_count(paths) == 0


def test_a_non_json_body_is_refused(listener) -> None:
    paths, base = listener
    status, _ = _post(f"{base}/hooks/publish_result", b"not json at all")
    assert status == 400
    assert pending_count(paths) == 0


def test_a_json_array_is_refused(listener) -> None:
    """The queue stores an object; accepting a bare array would produce a
    payload no workflow could reference."""
    paths, base = listener
    status, _ = _post(f"{base}/hooks/publish_result", b"[1,2,3]")
    assert status == 400
    assert pending_count(paths) == 0


def test_an_unknown_path_is_not_found(listener) -> None:
    _paths, base = listener
    status, _ = _post(f"{base}/wat", {"ok": True})
    assert status == 404


def test_the_server_does_not_advertise_its_python_version(listener) -> None:
    """Free reconnaissance otherwise."""
    _paths, base = listener
    request = urllib.request.Request(f"{base}/hooks/x", data=b"{}", method="POST")
    request.add_header("Authorization", f"Bearer {KEY}")
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        server_header = response.headers.get("Server", "")
    assert "Python" not in server_header


# --- backpressure ----------------------------------------------------------------


def test_a_full_queue_answers_retryable_rather_than_dropping(listener, tmp_path: Path) -> None:
    """The sender must be told to come back. A silent drop loses the event while
    the provider records a success."""
    paths, base = listener
    config = tmp_path / "config.yaml"
    data = read_yaml(config)
    data["events"] = {"max_pending": 1}
    write_yaml(config, data)

    _post(f"{base}/hooks/publish_result", {"n": 1})
    status, body = _post(f"{base}/hooks/publish_result", {"n": 2})

    assert status == 503 and "retry" in body["error"]
    assert stats(paths)["pending"] == 1


# --- end to end -------------------------------------------------------------------


def test_a_webhook_runs_only_an_opted_in_workflow(listener, tmp_path: Path) -> None:
    """The full path: HTTP in, queue, drain, run — and the targeting boundary
    still holds at the far end."""
    from jigga.core.config import load_workflows
    from jigga.runtime.supervisor import _drain_event_queue

    paths, base = listener
    write_yaml(paths.agents / "worker.yaml",
               {"id": "worker", "name": "worker", "role": "r", "tools": ["summarize"],
                "permissions": {"memory": {"scope": "task_only"}}, "memory_scope": "task_only"})
    write_yaml(paths.workflows / "opted_in.yaml", {
        "id": "opted_in", "name": "opted_in", "trigger": {"webhook": "opted_in"},
        "steps": [{"id": "s1", "agent": "worker", "action": "summarize"}]})
    write_yaml(paths.workflows / "internal.yaml", {
        "id": "internal", "name": "internal", "trigger": {"schedule": "weekdays at 09:00"},
        "steps": [{"id": "s1", "agent": "worker", "action": "summarize"}]})

    _post(f"{base}/hooks/opted_in", {"workflow": "opted_in"})
    _post(f"{base}/hooks/internal", {"workflow": "internal"})

    ran = _drain_event_queue(paths, load_workflows(paths.workflows))

    assert len(ran) == 1, "only the opted-in workflow should have run"
    assert stats(paths)["failed"] == 1, "the non-opted-in event should be parked, not run"
