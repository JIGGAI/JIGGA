"""SMS — the driver seam, and the three failures it exists to prevent.

Every test here maps to a dated incident on the precursor stack
(FIELD_LESSONS §3.7). The provider is deliberately absent: the seam ships with a
dry-run driver so routing, consent, and delivery state are pinned before any
real account is wired, and a provider driver drops into `SMS_DRIVERS`.
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import read_json, write_json
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.channels import ADAPTERS
from jigga.runtime.sms import (
    ACCEPTED,
    DELIVERED,
    SUPPRESSED,
    SmsAdapter,
    clear_suppression,
    default_number,
    delivery_state,
    record_receipt,
    record_suppression,
    send_sms,
    suppression,
)

MARKETING = "+15550000001"
OPERATIONS = "+15550000002"
CLIENT = "+15559999999"


def _configure(paths) -> None:
    config = read_yaml(paths.config)
    channels = dict(config.get("channels") or {})
    channels["sms"] = {
        "enabled": True,
        "provider": "dry_run",
        "numbers": {
            MARKETING: {"purpose": "client marketing", "default_agent": "marketing_lead"},
            OPERATIONS: {"purpose": "field operations", "default_agent": "ops_lead"},
        },
    }
    config["channels"] = channels
    write_yaml(paths.config, config)


def _inbox(paths, messages: list[dict]) -> None:
    (paths.home / "sms").mkdir(exist_ok=True)
    write_json(paths.home / "sms" / "inbox.json", messages)


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# --- registration -----------------------------------------------------------


def test_sms_is_a_registered_channel() -> None:
    assert "sms" in ADAPTERS
    adapter = ADAPTERS["sms"]
    assert adapter.name == "sms"
    # Drivers fetch and return; claiming to long-poll would hot-spin the loop.
    assert adapter.long_polls is False


# --- accepted is not delivered (assertion 20) -------------------------------


def test_a_send_reports_accepted_never_delivered(tmp_path: Path) -> None:
    """The old path logged `sent` on an API 200 and had no receipt tracking at
    all, so the log said `sent` for messages the carrier was blocking."""
    paths = init_runtime(tmp_path)
    _configure(paths)
    result = send_sms(paths.home, destination=CLIENT, text="hi",
                      source=MARKETING, logs_dir=paths.logs)
    assert result["status"] == ACCEPTED
    assert result["delivered"] is None          # not False — we don't know either way
    assert result["reports_delivery"] is False  # this driver cannot ever know
    assert result["provider_message_id"]
    assert [e["type"] for e in _events(paths) if e["type"].startswith("sms.")] == ["sms.accepted"]


def test_delivered_comes_only_from_a_receipt(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(paths)
    sent = send_sms(paths.home, destination=CLIENT, text="hi", source=MARKETING)
    message_id = sent["provider_message_id"]
    assert delivery_state(paths.home, message_id) == ACCEPTED

    record_receipt(paths.home, provider_message_id=message_id, status=DELIVERED,
                   logs_dir=paths.logs)
    assert delivery_state(paths.home, message_id) == DELIVERED
    assert any(e["type"] == "sms.delivered" for e in _events(paths))


def test_a_failed_receipt_is_an_error_event(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(paths)
    sent = send_sms(paths.home, destination=CLIENT, text="hi", source=MARKETING)
    record_receipt(paths.home, provider_message_id=sent["provider_message_id"],
                   status="failed", detail="carrier rejected", logs_dir=paths.logs)
    failed = [e for e in _events(paths) if e["type"] == "sms.failed"]
    assert failed and failed[-1]["status"] == "error"
    assert failed[-1]["details"]["detail"] == "carrier rejected"


def test_an_unknown_message_id_reads_as_accepted(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    assert delivery_state(paths.home, "never-sent") == ACCEPTED


# --- provider suppression outranks local state ------------------------------


def test_a_suppressed_pair_is_never_sent_to(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(paths)
    record_suppression(paths.home, source=MARKETING, destination=CLIENT, origin="provider")
    result = send_sms(paths.home, destination=CLIENT, text="hi", source=MARKETING,
                      logs_dir=paths.logs)
    assert result["status"] == SUPPRESSED
    assert result["delivered"] is False
    assert not (paths.home / "sms" / "outbox.json").exists()   # never handed over
    assert any(e["type"] == "sms.send_blocked" for e in _events(paths))


def test_suppression_is_per_source_number_not_per_recipient(tmp_path: Path) -> None:
    """The carrier keys opt-out to the (source, destination) pair. Someone who
    stopped marketing must still receive operations messages."""
    paths = init_runtime(tmp_path)
    _configure(paths)
    record_suppression(paths.home, source=MARKETING, destination=CLIENT, origin="provider")
    assert suppression(paths.home, source=MARKETING, destination=CLIENT) is not None
    assert suppression(paths.home, source=OPERATIONS, destination=CLIENT) is None
    assert send_sms(paths.home, destination=CLIENT, text="shift change",
                    source=OPERATIONS)["status"] == ACCEPTED


def test_a_carrier_suppression_cannot_be_cleared_locally(tmp_path: Path) -> None:
    """Deleting the local row looked like it worked and changed nothing — the
    carrier kept blocking, and the silence read as 'no messages to send' for a
    week."""
    paths = init_runtime(tmp_path)
    _configure(paths)
    record_suppression(paths.home, source=MARKETING, destination=CLIENT, origin="provider")
    result = clear_suppression(paths.home, source=MARKETING, destination=CLIENT)
    assert result["cleared"] is False
    assert "text START" in result["reason"]
    assert MARKETING in result["reason"]                     # names the number to text
    assert suppression(paths.home, source=MARKETING, destination=CLIENT) is not None


def test_a_local_opt_out_can_be_cleared(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(paths)
    record_suppression(paths.home, source=MARKETING, destination=CLIENT, origin="local")
    assert clear_suppression(paths.home, source=MARKETING, destination=CLIENT,
                             logs_dir=paths.logs)["cleared"] is True
    assert suppression(paths.home, source=MARKETING, destination=CLIENT) is None


# --- inbound routes by destination (assertion 21) ---------------------------


def test_inbound_is_routed_by_the_number_it_arrived_at(tmp_path: Path) -> None:
    """7 of 10 historical inbounds were misrouted because everything was treated
    as client-marketing regardless of destination."""
    paths = init_runtime(tmp_path)
    _configure(paths)
    _inbox(paths, [
        {"to": OPERATIONS, "from": CLIENT, "text": "running late to the Dearborn shop"},
        {"to": MARKETING, "from": CLIENT, "text": "do you have Saturday slots?"},
    ])
    events = SmsAdapter().poll(paths.home)["events"]
    assert [e.target["agent"] for e in events] == ["ops_lead", "marketing_lead"]
    assert [e.raw["purpose"] for e in events] == ["field operations", "client marketing"]


def test_the_same_handset_on_two_numbers_is_two_conversations(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(paths)
    _inbox(paths, [{"to": OPERATIONS, "from": CLIENT, "text": "a"},
                   {"to": MARKETING, "from": CLIENT, "text": "b"}])
    ids = [e.conversation_id for e in SmsAdapter().poll(paths.home)["events"]]
    assert ids == [f"{OPERATIONS}:{CLIENT}", f"{MARKETING}:{CLIENT}"]
    assert len(set(ids)) == 2


def test_an_unconfigured_destination_still_yields_an_event(tmp_path: Path) -> None:
    """Unknown number: no pre-targeting, but the message is not dropped — the
    listener falls back to the channel default."""
    paths = init_runtime(tmp_path)
    _configure(paths)
    _inbox(paths, [{"to": "+15550000009", "from": CLIENT, "text": "hello?"}])
    events = SmsAdapter().poll(paths.home)["events"]
    assert len(events) == 1
    assert events[0].target == {}


# --- consent is not conversation --------------------------------------------


def test_stop_suppresses_only_the_pair_it_arrived_on(tmp_path: Path) -> None:
    """A STOP from an employee to the operations number must not opt them out of
    marketing — and must not become a task for an agent to answer."""
    paths = init_runtime(tmp_path)
    _configure(paths)
    _inbox(paths, [{"to": OPERATIONS, "from": CLIENT, "text": "STOP"}])
    assert SmsAdapter().poll(paths.home)["events"] == []
    assert suppression(paths.home, source=OPERATIONS, destination=CLIENT) is not None
    assert suppression(paths.home, source=MARKETING, destination=CLIENT) is None


def test_start_clears_a_local_stop(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(paths)
    _inbox(paths, [{"to": MARKETING, "from": CLIENT, "text": "stop"}])
    SmsAdapter().poll(paths.home)
    _inbox(paths, [{"to": MARKETING, "from": CLIENT, "text": "START"}])
    assert SmsAdapter().poll(paths.home)["events"] == []
    assert suppression(paths.home, source=MARKETING, destination=CLIENT) is None


# --- the seam ---------------------------------------------------------------


def test_a_provider_fault_does_not_break_the_poll(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    config = read_yaml(paths.config)
    config["channels"] = {"sms": {"enabled": True, "provider": "nonesuch"}}
    write_yaml(paths.config, config)
    result = SmsAdapter().poll(paths.home)
    assert result["events"] == []
    assert "nonesuch" in result["status"]


def test_a_new_provider_is_one_registry_entry(tmp_path: Path) -> None:
    """Multitel and friends drop in here — nothing else changes."""
    from jigga.runtime.sms import SMS_DRIVERS

    class _Fake:
        name = "fake"
        reports_delivery = True

        def send(self, home, *, source, destination, text):
            return {"provider_message_id": "fake-1"}

        def fetch(self, home):
            return [{"to": MARKETING, "from": CLIENT, "text": "via fake"}]

    paths = init_runtime(tmp_path)
    _configure(paths)
    config = read_yaml(paths.config)
    config["channels"]["sms"]["provider"] = "fake"
    write_yaml(paths.config, config)
    SMS_DRIVERS["fake"] = _Fake
    try:
        sent = send_sms(paths.home, destination=CLIENT, text="hi", source=MARKETING)
        assert sent["provider"] == "fake"
        # A driver that CAN observe receipts says so, and the runtime reports it.
        assert sent["reports_delivery"] is True
        assert [e.text for e in SmsAdapter().poll(paths.home)["events"]] == ["via fake"]
    finally:
        SMS_DRIVERS.pop("fake", None)


def test_sending_without_a_configured_number_is_an_error(tmp_path: Path) -> None:
    import pytest

    paths = init_runtime(tmp_path)
    with pytest.raises(ValueError, match="no SMS source number"):
        send_sms(paths.home, destination=CLIENT, text="hi")


def test_the_adapter_replies_on_the_number_it_was_reached_at(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(paths)
    result = SmsAdapter().send(paths.home, conversation_id=f"{OPERATIONS}:{CLIENT}",
                               text="on my way")
    assert result["source"] == OPERATIONS         # not the default/marketing number
    assert result["destination"] == CLIENT
    assert read_json(paths.home / "sms" / "outbox.json")[0]["source"] == OPERATIONS


def test_default_number_falls_back_to_the_first_configured(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(paths)
    assert default_number(paths.home) == MARKETING
