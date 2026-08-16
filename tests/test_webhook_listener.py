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
    # `source` is the authenticated caller. This fixture uses the legacy single
    # shared key, which reports as "shared" rather than naming a third party.
    assert record["source"] == "shared"


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


# --- per-caller keys: JIGGA is the provider issuing credentials -----------------


@pytest.fixture
def multi_caller(tmp_path: Path):
    """Two third parties, each issued its own key — no shared key at all."""
    paths = init_runtime(tmp_path)
    _configure(tmp_path)
    set_secret(tmp_path, "webhook_key@postiz", "postiz-key-111")
    set_secret(tmp_path, "webhook_key@multitel", "multitel-key-222")
    server = build_server(paths)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield paths, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_issued_keys_start_the_listener_without_any_shared_key(multi_caller) -> None:
    """A provider issues per-caller credentials; requiring a shared key as well
    would defeat the point."""
    paths, base = multi_caller
    status, _ = _post(f"{base}/hooks/publish_result", {"ok": True}, key="postiz-key-111")
    assert status == 202


def test_the_authenticated_caller_is_recorded_not_the_word_webhook(multi_caller) -> None:
    """Attribution: the audit trail and the queue record must say WHICH third
    party called. With one shared key that question had no answer."""
    from jigga.runtime.event_queue import claim

    paths, base = multi_caller
    _post(f"{base}/hooks/publish_result", {"ok": True}, key="multitel-key-222")

    _path, record = next(iter(claim(paths, 1)))
    assert record["source"] == "multitel"


def test_each_caller_is_distinguished(multi_caller) -> None:
    from jigga.runtime.event_queue import claim

    paths, base = multi_caller
    _post(f"{base}/hooks/a", {"n": 1}, key="postiz-key-111")
    _post(f"{base}/hooks/b", {"n": 2}, key="multitel-key-222")

    sources = sorted(record["source"] for _p, record in claim(paths, 10))
    assert sources == ["multitel", "postiz"]


def test_revoking_one_caller_leaves_the_others_working(tmp_path: Path) -> None:
    """The reason per-caller keys exist: one integration's leak must not force
    a rotation that breaks every other integration."""
    from jigga.runtime.secrets_broker import delete_secret
    from jigga.runtime.webhook import identify_caller, issued_keys

    paths = init_runtime(tmp_path)
    set_secret(tmp_path, "webhook_key@postiz", "postiz-key-111")
    set_secret(tmp_path, "webhook_key@multitel", "multitel-key-222")

    delete_secret(tmp_path, "webhook_key@postiz")

    keys = issued_keys(paths)
    assert identify_caller(keys, "postiz-key-111") is None
    assert identify_caller(keys, "multitel-key-222") == "multitel"


def test_an_unknown_key_identifies_nobody(tmp_path: Path) -> None:
    from jigga.runtime.webhook import identify_caller

    assert identify_caller({"postiz": "abc"}, "xyz") is None


def test_the_listener_reports_which_callers_it_will_accept(tmp_path: Path) -> None:
    """Operationally: after a restart you want to know which integrations are
    live without reading the secrets directory.

    Goes through `serve_in_background` rather than the fixture, because that is
    the path the supervisor actually uses and the one that emits the event.
    """
    import json

    paths = init_runtime(tmp_path)
    _configure(tmp_path)
    set_secret(tmp_path, "webhook_key@postiz", "postiz-key-111")
    set_secret(tmp_path, "webhook_key@multitel", "multitel-key-222")

    started = serve_in_background(paths)
    assert started is not None
    server, _thread = started
    try:
        rows = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()
                if line.strip()]
        listening = [r for r in rows if r["type"] == "webhook.listening"]
        assert listening and listening[-1]["details"]["callers"] == ["multitel", "postiz"]
        assert listening[-1]["details"]["shared_key"] is False
    finally:
        server.shutdown()
        server.server_close()


# --- a workflow can require a specific caller -----------------------------------


def test_a_workflow_can_accept_events_from_only_one_caller(multi_caller) -> None:
    """Least privilege between third parties: a key issued to one integration
    must not be able to start another integration's workflow."""
    from jigga.core.config import load_workflows
    from jigga.runtime.event_queue import list_failed
    from jigga.runtime.supervisor import _drain_event_queue

    paths, base = multi_caller
    write_yaml(paths.agents / "worker.yaml",
               {"id": "worker", "name": "worker", "role": "r", "tools": ["summarize"],
                "permissions": {"memory": {"scope": "task_only"}}, "memory_scope": "task_only"})
    write_yaml(paths.workflows / "postiz_only.yaml", {
        "id": "postiz_only", "name": "postiz_only",
        "trigger": {"webhook": "postiz_only", "source": "postiz"},
        "steps": [{"id": "s1", "agent": "worker", "action": "summarize"}]})

    # The wrong third party's key, for a workflow that names another.
    _post(f"{base}/hooks/postiz_only", {"workflow": "postiz_only"}, key="multitel-key-222")
    ran = _drain_event_queue(paths, load_workflows(paths.workflows))

    assert ran == []
    assert "authenticated as 'multitel'" in list_failed(paths)[0]["error"]


def test_the_named_caller_is_allowed_through(multi_caller) -> None:
    from jigga.core.config import load_workflows
    from jigga.runtime.supervisor import _drain_event_queue

    paths, base = multi_caller
    write_yaml(paths.agents / "worker.yaml",
               {"id": "worker", "name": "worker", "role": "r", "tools": ["summarize"],
                "permissions": {"memory": {"scope": "task_only"}}, "memory_scope": "task_only"})
    write_yaml(paths.workflows / "postiz_only.yaml", {
        "id": "postiz_only", "name": "postiz_only",
        "trigger": {"webhook": "postiz_only", "source": "postiz"},
        "steps": [{"id": "s1", "agent": "worker", "action": "summarize"}]})

    _post(f"{base}/hooks/postiz_only", {"workflow": "postiz_only"}, key="postiz-key-111")

    assert len(_drain_event_queue(paths, load_workflows(paths.workflows))) == 1


def test_a_workflow_without_a_source_accepts_any_authenticated_caller(multi_caller) -> None:
    """`source:` is opt-in tightening, not a new requirement — omitting it must
    not break an existing hook."""
    from jigga.core.config import load_workflows
    from jigga.runtime.supervisor import _drain_event_queue

    paths, base = multi_caller
    write_yaml(paths.agents / "worker.yaml",
               {"id": "worker", "name": "worker", "role": "r", "tools": ["summarize"],
                "permissions": {"memory": {"scope": "task_only"}}, "memory_scope": "task_only"})
    write_yaml(paths.workflows / "open_hook.yaml", {
        "id": "open_hook", "name": "open_hook", "trigger": {"webhook": "open_hook"},
        "steps": [{"id": "s1", "agent": "worker", "action": "summarize"}]})

    _post(f"{base}/hooks/open_hook", {"workflow": "open_hook"}, key="multitel-key-222")

    assert len(_drain_event_queue(paths, load_workflows(paths.workflows))) == 1


# --- the provider surface: issuing and revoking keys -----------------------------


def _cli(tmp_path: Path, *args) -> tuple[int, str]:
    import contextlib
    import io

    from jigga.cli import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["--home", str(tmp_path), *args])
    return code, buf.getvalue()


def test_issuing_prints_the_key_once_and_stores_it(tmp_path: Path) -> None:
    from jigga.runtime.webhook import issued_keys

    paths = init_runtime(tmp_path)
    code, output = _cli(tmp_path, "webhook", "issue", "postiz")

    assert code == 0
    keys = issued_keys(paths)
    assert list(keys) == ["postiz"]
    assert keys["postiz"] in output, "the caller has to be told the key at least once"


def test_the_key_is_never_printed_again(tmp_path: Path) -> None:
    """A stored credential re-printed on demand turns every shell history and
    terminal scrollback into a copy of it."""
    from jigga.runtime.webhook import issued_keys

    paths = init_runtime(tmp_path)
    _cli(tmp_path, "webhook", "issue", "postiz")
    secret = issued_keys(paths)["postiz"]

    for command in (("webhook", "list"), ("webhook", "status")):
        _code, output = _cli(tmp_path, *command)
        assert secret not in output, f"{command} leaked the stored key"


def test_revoking_removes_only_that_caller(tmp_path: Path) -> None:
    from jigga.runtime.webhook import issued_keys

    paths = init_runtime(tmp_path)
    _cli(tmp_path, "webhook", "issue", "postiz")
    _cli(tmp_path, "webhook", "issue", "multitel")

    code, _ = _cli(tmp_path, "webhook", "revoke", "postiz")

    assert code == 0
    assert list(issued_keys(paths)) == ["multitel"]


def test_revoking_an_unknown_caller_is_reported(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    code, output = _cli(tmp_path, "webhook", "revoke", "nobody")
    assert code == 1 and "No key issued" in output


def test_a_caller_name_cannot_smuggle_a_scope_separator(tmp_path: Path) -> None:
    """`@` separates the name from the caller in storage; allowing it in the
    caller would let one issue collide with another."""
    init_runtime(tmp_path)
    code, _ = _cli(tmp_path, "webhook", "issue", "postiz@evil")
    assert code == 1


def test_status_flags_enabled_with_no_keys(tmp_path: Path) -> None:
    """The exact misconfiguration that leaves the listener refusing to start."""
    init_runtime(tmp_path)
    _configure(tmp_path)

    code, output = _cli(tmp_path, "webhook", "status")

    assert code == 1
    assert "refuses to start unauthenticated" in output


def test_status_warns_that_a_shared_key_loses_attribution(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    set_secret(tmp_path, SECRET_NAME, "legacy")
    _cli(tmp_path, "webhook", "issue", "postiz")

    _code, output = _cli(tmp_path, "webhook", "list")

    assert "legacy shared key" in output and "attribution" in output
