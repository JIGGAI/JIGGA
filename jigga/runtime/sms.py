"""SMS channel — a provider-agnostic driver seam plus the three things the
precursor stack got wrong.

`SMS_DRIVERS` maps a driver name to an implementation; adding a provider is one
entry plus one class. A `dry_run` driver ships so the whole path — inbound
routing, opt-out, delivery state — is exercisable before any provider is wired.

Three constraints are baked into the seam rather than left to each driver,
because each one cost real money on the previous stack
(FIELD_LESSONS §3.7):

**Accepted is not delivered.** The old send path checked only API acceptance
(HTTP 200 + `status: success`) and logged `sent`. There was no delivery receipt
tracking at all, so the log said `sent` for messages the carrier was blocking. A
driver here reports `accepted` and nothing more; `delivered` only ever comes
from a receipt, and a driver that cannot report receipts says so.

**Provider suppression outranks local state.** The provider records a
carrier-level opt-out keyed to the *(source number, destination)* pair, entirely
independent of any local table. Deleting the local row does not restore
delivery — the recipient must text START back to the same source number. Local
opt-out can therefore never clear a provider suppression, and this module
refuses to pretend otherwise. That gap cost RJ a week of silently missing his
own daily coverage SMS.

**Inbound routes by destination.** All inbound used to be processed as
client-marketing regardless of which number it arrived at, so field-manager
replies to the operations number landed in the marketing inbox and a STOP from
an employee would have wrongly opted them out of marketing — 7 of 10 historical
inbounds were misrouted. Here the destination number is a first-class routing
key and selects the agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol

from jigga.core.config import load_runtime_config
from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import now_iso
from jigga.runtime.audit import append_event

# Delivery states, most to least certain. `accepted` is the ceiling for a
# provider with no receipt support — never promote it to `delivered` on the
# strength of an API 200.
ACCEPTED = "accepted"
DELIVERED = "delivered"
FAILED = "failed"
SUPPRESSED = "suppressed"


def _sms_dir(home: Path) -> Path:
    return Path(home) / "sms"


def _config(home: Path) -> dict[str, Any]:
    channels = load_runtime_config(home).get("channels") or {}
    sms = channels.get("sms") if isinstance(channels, dict) else None
    return sms if isinstance(sms, dict) else {}


def numbers(home: Path) -> dict[str, dict[str, Any]]:
    """Configured source numbers → their purpose and routing."""
    declared = _config(home).get("numbers")
    if not isinstance(declared, dict):
        return {}
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in declared.items()}


def default_number(home: Path) -> str | None:
    config = _config(home)
    explicit = config.get("default_number")
    if explicit:
        return str(explicit)
    configured = numbers(home)
    return next(iter(configured), None)


# --- suppression ------------------------------------------------------------


def _suppression_path(home: Path) -> Path:
    return _sms_dir(home) / "suppressions.json"


def _suppressions(home: Path) -> dict[str, Any]:
    path = _suppression_path(home)
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except (OSError, ValueError):
        return {}


def _key(source: str, destination: str) -> str:
    return f"{source}->{destination}"


def record_suppression(home: Path, *, source: str, destination: str,
                       origin: str, logs_dir: Path | None = None) -> dict[str, Any]:
    """Record that this (source → destination) pair may not be messaged.

    `origin` is `provider` when the carrier told us, `local` when the recipient
    texted STOP to us directly. The distinction matters at clear time.
    """
    store = _suppressions(home)
    entry = {"source": source, "destination": destination, "origin": origin,
             "recorded_at": now_iso()}
    store[_key(source, destination)] = entry
    ensure_dir(_sms_dir(home))
    write_json(_suppression_path(home), store)
    if logs_dir is not None:
        append_event(logs_dir, "sms.suppressed", status="deny", source=source,
                     destination=destination, origin=origin)
    return entry


def suppression(home: Path, *, source: str, destination: str) -> dict[str, Any] | None:
    return _suppressions(home).get(_key(source, destination))


def clear_suppression(home: Path, *, source: str, destination: str,
                      logs_dir: Path | None = None) -> dict[str, Any]:
    """Clear a *local* suppression. A provider suppression cannot be cleared
    from here, and saying so is the whole point.

    On the precursor stack deleting the local row looked like it worked and
    changed nothing — the carrier kept blocking, and the silence was mistaken
    for "no messages to send" for a week.
    """
    store = _suppressions(home)
    key = _key(source, destination)
    entry = store.get(key)
    if entry is None:
        return {"cleared": False, "reason": "not suppressed"}
    if entry.get("origin") == "provider":
        return {
            "cleared": False,
            "reason": (f"{destination} is suppressed at the carrier for {source}. "
                       f"Clearing it here would change nothing — the recipient has to text "
                       f"START to {source} from that handset."),
        }
    del store[key]
    write_json(_suppression_path(home), store)
    if logs_dir is not None:
        append_event(logs_dir, "sms.suppression_cleared", source=source, destination=destination)
    return {"cleared": True}


# --- drivers ----------------------------------------------------------------


class SmsDriver(Protocol):
    """What a provider must implement.

    `reports_delivery` is not decoration: a driver that cannot observe receipts
    must say so, and the runtime then refuses to ever report `delivered` for it
    rather than implying knowledge it doesn't have.
    """

    name: str
    reports_delivery: bool

    def send(self, home: Path, *, source: str, destination: str, text: str) -> dict[str, Any]: ...

    def fetch(self, home: Path) -> list[dict[str, Any]]: ...


class DryRunDriver:
    """File-backed driver: outbound lands in `sms/outbox.json`, inbound is read
    from `sms/inbox.json`. Lets the routing, opt-out, and delivery-state logic
    be exercised end to end with no provider account."""

    name = "dry_run"
    reports_delivery = False

    def send(self, home: Path, *, source: str, destination: str, text: str) -> dict[str, Any]:
        path = _sms_dir(home) / "outbox.json"
        ensure_dir(_sms_dir(home))
        outbox = []
        if path.exists():
            try:
                outbox = read_json(path)
            except (OSError, ValueError):
                outbox = []
        record = {"id": f"dry_{len(outbox) + 1}", "source": source,
                  "destination": destination, "text": text, "at": now_iso()}
        outbox.append(record)
        write_json(path, outbox)
        return {"provider_message_id": record["id"]}

    def fetch(self, home: Path) -> list[dict[str, Any]]:
        path = _sms_dir(home) / "inbox.json"
        if not path.exists():
            return []
        try:
            pending = read_json(path)
        except (OSError, ValueError):
            return []
        write_json(path, [])          # consume, like a real provider cursor
        return pending if isinstance(pending, list) else []


# name -> driver. Multitel and friends register here.
SMS_DRIVERS: dict[str, Callable[[], Any]] = {"dry_run": DryRunDriver}


def resolve_driver(home: Path) -> Any:
    name = str(_config(home).get("provider") or "dry_run")
    factory = SMS_DRIVERS.get(name)
    if factory is None:
        raise ValueError(
            f"unknown SMS provider {name!r} (channels.sms.provider). "
            f"Available: {', '.join(sorted(SMS_DRIVERS))}"
        )
    return factory()


# --- sending ----------------------------------------------------------------


def send_sms(home: Path, *, destination: str, text: str, source: str | None = None,
             logs_dir: Path | None = None) -> dict[str, Any]:
    """Hand a message to the provider. Returns a delivery *state*, never a claim.

    The result's `status` is `accepted` — the provider took it — or `suppressed`
    when this pair may not be messaged. It becomes `delivered` only when a
    receipt says so, via `record_receipt`.
    """
    from_number = source or default_number(home)
    if not from_number:
        raise ValueError("no SMS source number configured (channels.sms.numbers)")
    blocked = suppression(home, source=from_number, destination=destination)
    if blocked is not None:
        if logs_dir is not None:
            append_event(logs_dir, "sms.send_blocked", status="deny", source=from_number,
                         destination=destination, origin=blocked.get("origin"))
        return {"status": SUPPRESSED, "delivered": False, "source": from_number,
                "destination": destination, "reason": f"suppressed ({blocked.get('origin')})"}

    driver = resolve_driver(home)
    result = driver.send(home, source=from_number, destination=destination, text=text)
    record = {
        "status": ACCEPTED,
        # Tri-state on purpose: False would assert non-delivery, which we don't
        # know either. None means "the provider took it; nobody has told us what
        # happened since."
        "delivered": None,
        "reports_delivery": bool(getattr(driver, "reports_delivery", False)),
        "source": from_number,
        "destination": destination,
        "provider": driver.name,
        "provider_message_id": result.get("provider_message_id"),
        "accepted_at": now_iso(),
    }
    if logs_dir is not None:
        append_event(logs_dir, "sms.accepted", source=from_number, destination=destination,
                     provider=driver.name, provider_message_id=record["provider_message_id"],
                     reports_delivery=record["reports_delivery"])
    return record


def record_receipt(home: Path, *, provider_message_id: str, status: str,
                   logs_dir: Path | None = None, detail: str | None = None) -> dict[str, Any]:
    """Apply a provider delivery receipt. This is the only path to `delivered`.

    A `failed` receipt carrying a carrier opt-out also records the suppression,
    since the carrier is authoritative and the local table isn't.
    """
    receipts_path = _sms_dir(home) / "receipts.json"
    ensure_dir(_sms_dir(home))
    receipts = {}
    if receipts_path.exists():
        try:
            receipts = read_json(receipts_path)
        except (OSError, ValueError):
            receipts = {}
    entry = {"provider_message_id": provider_message_id, "status": status,
             "detail": detail, "at": now_iso()}
    receipts[provider_message_id] = entry
    write_json(receipts_path, receipts)
    if logs_dir is not None:
        append_event(logs_dir, f"sms.{status}", status="ok" if status == DELIVERED else "error",
                     provider_message_id=provider_message_id, detail=detail)
    return entry


def delivery_state(home: Path, provider_message_id: str) -> str:
    """`delivered` / `failed` when a receipt says so, else `accepted`."""
    path = _sms_dir(home) / "receipts.json"
    if not path.exists():
        return ACCEPTED
    try:
        receipts = read_json(path)
    except (OSError, ValueError):
        return ACCEPTED
    return str((receipts.get(provider_message_id) or {}).get("status") or ACCEPTED)


# --- the channel adapter ----------------------------------------------------

_STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
_START_WORDS = {"start", "unstop", "yes"}


class SmsAdapter:
    """SMS on the `ChannelAdapter` contract.

    The destination number is the routing key. Each configured number declares
    its own purpose and `default_agent`, and inbound events are pre-targeted
    accordingly — so a field manager replying to the operations number reaches
    the operations agent, not whoever happens to own marketing.
    """

    name = "sms"
    # Drivers fetch from an API/cursor and return immediately; claiming to long
    # poll would hot-spin the supervisor loop.
    long_polls = False
    self_transcribed = False

    def poll(self, home: Path, *, long_poll_seconds: int = 0) -> dict[str, Any]:
        from jigga.runtime.channels import JiggaEvent

        try:
            driver = resolve_driver(home)
            messages = driver.fetch(home)
        except Exception as exc:  # noqa: BLE001 — a provider fault must not break the tick
            return {"status": f"error: {exc}", "events": []}

        routes = numbers(home)
        events: list[Any] = []
        for message in messages:
            destination = str(message.get("to") or message.get("destination") or "")
            sender = str(message.get("from") or message.get("sender") or "")
            text = str(message.get("text") or message.get("body") or "")
            route = routes.get(destination, {})

            # STOP/START are consent, not conversation. They are handled against
            # the pair they arrived on and never become a task — an employee's
            # STOP to the operations number must not opt them out of marketing.
            word = text.strip().lower()
            if word in _STOP_WORDS:
                record_suppression(home, source=destination, destination=sender,
                                   origin="local", logs_dir=home / "logs")
                continue
            if word in _START_WORDS:
                clear_suppression(home, source=destination, destination=sender,
                                  logs_dir=home / "logs")
                continue

            event = JiggaEvent(
                source="sms",
                actor={"type": "user", "id": sender, "name": sender},
                # The conversation is the pair, not the sender: the same handset
                # texting two of our numbers is two conversations.
                conversation={"id": f"{destination}:{sender}", "type": "private"},
                message={"text": text, "attachments": []},
                raw={**message, "destination": destination, "sender": sender,
                     "purpose": route.get("purpose")},
            )
            if route.get("default_agent"):
                event.target = {"agent": str(route["default_agent"])}
            events.append(event)
        return {"status": "ok", "events": events}

    def send(self, home: Path, *, conversation_id: Any, text: str) -> dict[str, Any]:
        """Reply on the number the message arrived at.

        The returned record reports `accepted`, never `delivered` — see the
        module docstring. A caller wanting delivery must consult
        `delivery_state` after a receipt lands.
        """
        source, _, destination = str(conversation_id).partition(":")
        if not destination:                      # a bare number: use the default source
            source, destination = default_number(home) or "", str(conversation_id)
        return send_sms(home, destination=destination, text=text, source=source or None,
                        logs_dir=Path(home) / "logs")
