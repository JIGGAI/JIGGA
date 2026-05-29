"""Telegram channel first-party optional capability.

`setup(paths, ...)` is invoked by `jigga capabilities install telegram`. It:

  1. Collects a Telegram bot token (from @BotFather) → ~/.jigga/secrets/.
  2. Optionally runs a discovery poll so the user can find their chat ID by
     messaging the bot first.
  3. Collects allowed chat IDs and writes
     `channels.telegram.{enabled, allowed_chat_ids, default_agent}` into
     config.yaml.

I/O is parameterised (input_fn / print_fn / poller) so the wizard is testable
without a real bot or network. This is also the template for adding more
channels (Slack, iMessage) — see docs/CHANNELS_TELEGRAM_RUNTIME_NOTES.md.
"""

from __future__ import annotations

from typing import Callable

from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.telegram import poll_messages, store_bot_token

_HELP = """
JIGGA talks to Telegram through a bot you own.

  1. In Telegram, message @BotFather and run /newbot — follow the prompts.
  2. BotFather gives you a token like 123456789:AAEx....
  3. Message your new bot anything (so it has an update to read).
"""


def _write_telegram_config(config_path, *, allowed_chat_ids: list[str], default_agent: str) -> None:
    config = read_yaml(config_path) if config_path.exists() else {}
    channels = dict(config.get("channels") or {})
    channels["telegram"] = {
        "enabled": True,
        "allowed_chat_ids": allowed_chat_ids,
        "default_agent": default_agent,
    }
    config["channels"] = channels
    write_yaml(config_path, config)


def setup(
    paths,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
    poller: Callable[..., dict] = poll_messages,
) -> int:
    """Interactive Telegram setup. Returns 0 on success."""
    print_fn("\n=== Telegram channel setup ===")
    print_fn(_HELP)

    token = ""
    while not token:
        token = input_fn("Paste your Telegram bot token (or 'q' to abort): ").strip()
        if token.lower() in {"q", "quit", "exit"}:
            print_fn("Aborted. Re-run `jigga capabilities install telegram` when ready.")
            return 1
    store_bot_token(paths.secrets, token)
    print_fn("Bot token stored.")

    # Optional discovery: poll once (allowlist bypassed) so the user can find
    # the chat ID to allowlist. Requires they've already messaged the bot.
    discovered: list[str] = []
    answer = input_fn("Discover your chat ID now? (message the bot first) [y/N]: ").strip().lower()
    if answer in {"y", "yes"}:
        try:
            result = poller(paths.home, discover=True)
            for message in result.get("messages", []):
                chat_id = message.get("chat_id")
                sender = message.get("sender")
                if chat_id is not None:
                    discovered.append(str(chat_id))
                    print_fn(f"  Found chat {chat_id} from {sender}")
            if not discovered:
                print_fn("  No messages found. Message your bot, then add the chat ID manually below.")
        except (RuntimeError, OSError) as exc:
            print_fn(f"  Discovery failed ({exc}); add chat IDs manually below.")

    default_ids = ",".join(dict.fromkeys(discovered))
    prompt = (
        f"Allowed chat IDs (comma-separated) [{default_ids}]: "
        if default_ids
        else "Allowed chat IDs (comma-separated, who may talk to JIGGA): "
    )
    raw = input_fn(prompt).strip()
    chat_ids = [c.strip() for c in (raw or default_ids).split(",") if c.strip()]
    if not chat_ids:
        print_fn(
            "No allowed chat IDs set. Inbound polling will default-deny until you add "
            "channels.telegram.allowed_chat_ids to config.yaml."
        )

    default_agent = input_fn("Default agent to route messages to [daily_briefing_agent]: ").strip() or "daily_briefing_agent"
    _write_telegram_config(paths.config, allowed_chat_ids=chat_ids, default_agent=default_agent)
    print_fn("\nTelegram channel configured. Try the examples/demos/telegram_echo.yaml workflow.")
    return 0
