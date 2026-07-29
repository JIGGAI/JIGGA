"""Per-invocation egress proxy — Milestone E slice E3a.

A localhost HTTP/HTTPS proxy that lives exactly as long as one sandboxed
subprocess and only relays to hosts on that capability's allowlist. The
subprocess gets `HTTP_PROXY`/`HTTPS_PROXY` pointed here; combined with the
bwrap backend (whose restricted env is kernel-enforced) this bounds the
realistic exfil paths — HTTP APIs — for the tools we run.

Honest about its bounds (`docs/MILESTONE_E_DESIGN.md`):
- CONNECT sees hostnames, never TLS content — this is allowlisting, not
  inspection.
- A process that speaks raw non-HTTP sockets bypasses an env proxy; the hard
  stop for those is `network: false` (`--unshare-net`). Proxy enforcement is
  *hard* only when the sandbox backend prevents direct egress; otherwise it
  is soft enforcement plus an audit signal.
- Every decision is audited: `egress.allowed` / `egress.blocked` with host
  and label only — the first observed-behavior-vs-declared-manifest signal
  (E3b reads these).

stdlib only: http.server + socket + threading.
"""

from __future__ import annotations

import http.server
import select
import socket
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_TUNNEL_IDLE_TIMEOUT = 300
_CHUNK = 65536


def _host_allowed(host: str, allowed: list[str]) -> bool:
    host = (host or "").lower()
    for entry in allowed:
        e = str(entry).strip().lower()
        # Accept bare hosts or URLs (the manifest shapes we already have).
        if "://" in e:
            e = urllib.parse.urlparse(e).hostname or e
        if not e:
            continue
        if e in ("*", "all") or host == e:
            return True
        if e.startswith("*.") and (host == e[2:] or host.endswith(e[1:])):
            return True
    return False


class EgressProxy:
    """One proxy instance per sandboxed invocation. `start()` returns the
    port; `stop()` tears everything down (daemon threads die with us)."""

    def __init__(self, allowed_hosts: list[str], *, logs_dir: Path | None = None,
                 label: str | None = None) -> None:
        self.allowed_hosts = list(allowed_hosts)
        self.logs_dir = Path(logs_dir) if logs_dir else None
        self.label = label
        self._server: http.server.ThreadingHTTPServer | None = None
        self.decisions: list[dict[str, Any]] = []  # in-memory mirror (tests/inspection)

    # --- audit --------------------------------------------------------------

    def _record(self, host: str, allowed: bool) -> None:
        self.decisions.append({"host": host, "allowed": allowed})
        if self.logs_dir is not None:
            from jigga.runtime.audit import append_event

            append_event(self.logs_dir, "egress.allowed" if allowed else "egress.blocked",
                         status="ok" if allowed else "denied", host=host, label=self.label)

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> int:
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a) -> None:  # quiet — the audit log is the record
                pass

            def do_CONNECT(self) -> None:  # noqa: N802 — http.server naming contract
                host, _, port = self.path.partition(":")
                if not _host_allowed(host, proxy.allowed_hosts):
                    proxy._record(host, False)
                    self.send_error(403, f"egress to {host!r} is not in this capability's allowlist")
                    return
                try:
                    upstream = socket.create_connection((host, int(port or 443)), timeout=30)
                except OSError as exc:
                    proxy._record(host, False)
                    self.send_error(502, f"connect failed: {exc}")
                    return
                proxy._record(host, True)
                self.send_response(200, "Connection Established")
                self.end_headers()
                self._pump(self.connection, upstream)

            def _pump(self, client: socket.socket, upstream: socket.socket) -> None:
                sockets = [client, upstream]
                try:
                    while True:
                        readable, _, errored = select.select(sockets, [], sockets, _TUNNEL_IDLE_TIMEOUT)
                        if errored or not readable:
                            return
                        for side in readable:
                            data = side.recv(_CHUNK)
                            if not data:
                                return
                            (upstream if side is client else client).sendall(data)
                except OSError:
                    return
                finally:
                    upstream.close()

            def do_GET(self) -> None:  # noqa: N802
                self._absolute_form()

            def do_HEAD(self) -> None:  # noqa: N802
                self._absolute_form()

            def _absolute_form(self) -> None:
                """Plain-HTTP proxying (absolute-URI request line)."""
                host = urllib.parse.urlparse(self.path).hostname or ""
                if not _host_allowed(host, proxy.allowed_hosts):
                    proxy._record(host, False)
                    self.send_error(403, f"egress to {host!r} is not in this capability's allowlist")
                    return
                proxy._record(host, True)
                try:
                    request = urllib.request.Request(self.path, method=self.command)
                    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 — allowlisted above
                        body = response.read(10_000_000)
                        self.send_response(response.status)
                        self.send_header("Content-Length", str(len(body)))
                        content_type = response.headers.get("Content-Type")
                        if content_type:
                            self.send_header("Content-Type", content_type)
                        self.end_headers()
                        if self.command != "HEAD":
                            self.wfile.write(body)
                except Exception as exc:  # noqa: BLE001 — any upstream fault becomes a proxy 502, never a crash
                    self.send_error(502, str(exc)[:200])

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        return self._server.server_address[1]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
