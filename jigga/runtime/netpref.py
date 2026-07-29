"""IPv4-first address resolution — resilience against broken IPv6 routes.

Home LANs grow blackholed IPv6 router advertisements (live incident
2026-07-29: a rogue fd01:: ULA prefix turned every Python connection into a
~90s IPv6-timeout-then-IPv4-fallback while curl's happy-eyeballs masked it).
Python's urllib tries getaddrinfo results in order, so sorting IPv4 first
gives happy-eyeballs-like behavior: fast when v6 is broken, v6 still used as
fallback when v4 fails. Default ON; disable with `network.prefer_ipv4: false`.
"""

from __future__ import annotations

import socket
from pathlib import Path

_original_getaddrinfo = socket.getaddrinfo
_installed = False


def install_ipv4_preference(home: Path | None = None) -> bool:
    """Idempotently sort IPv4 results first in socket.getaddrinfo (config-gated)."""
    global _installed
    if _installed:
        return True
    from jigga.core.config import load_runtime_config
    from jigga.core.paths import resolve_home

    try:
        network = load_runtime_config(resolve_home(home)).get("network") or {}
    except Exception:  # noqa: BLE001 — resolution preference must never block startup
        network = {}
    if network.get("prefer_ipv4", True) is False:
        return False

    def _v4_first(*args, **kwargs):
        infos = _original_getaddrinfo(*args, **kwargs)
        return sorted(infos, key=lambda info: 0 if info[0] == socket.AF_INET else 1)

    socket.getaddrinfo = _v4_first
    _installed = True
    return True
