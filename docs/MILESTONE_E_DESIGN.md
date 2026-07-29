# Milestone E — Real Isolation: Design (2026-07-29)

The last milestone before v1.0 (`ROADMAP_TO_PRODUCTION.md`). Goal, in the
roadmap's words: *"even a capability marked `risk_level: high` can be approved
knowing the worst case is bounded."* Today every bound is **Python-level** —
policy evaluators, env scrubbing, path canonicalization. They stop honest code
from doing the wrong thing; they do not stop a malicious capability pack, MCP
server, or compromised CLI from ignoring them. Milestone E moves the bounds to
the **OS**.

Three components, sequenced so each ships alone. Design before code — the
choices here compound everywhere.

## Non-goals

- Windows (lags a release, per roadmap Decision Point 2).
- Containers/Docker (heavier than needed; `bwrap` is the Linux target).
- Sandboxing local UX tools (`notify-send` etc.) — the routing rule in
  `runtime/sandbox.py` stands: they keep the session env, they don't act with
  the agent's authority on external systems.
- In-process isolation of JIGGA's own Python (that's the trust root; if the
  runtime itself is malicious, no self-applied sandbox helps).

## Where we are (the seams already exist)

- `runtime/sandbox.py`: `SandboxSpec` + `run_sandboxed()` — every
  external-authority subprocess (codex/claude subagents, MCP servers) already
  goes through one function whose docstring promises exactly this upgrade.
  `shell.run` (#156) uses `tools/safe_process.py`, which should converge onto
  the same seam as part of E2.
- Secrets: per-capability 0600 files under `~/.jigga/secrets/`
  (`chatgpt_auth.json`, `telegram_bot_token`, `email_imap.json`,
  `brave_api_key`) read directly by each module, plus
  `SandboxSpec.secrets_required` env passthrough. No central broker, no
  keychain, and the roadmap risk register notes `secrets_required` trusts the
  manifest.
- Egress: Python-level only — `evaluate_network` per-target allowlists,
  `web.allowed_domains`. A subprocess can ignore all of it.

## E1 — Secrets broker

**Shape.** A single module `runtime/secrets_broker.py` behind which ALL secret
reads happen. Named secrets, pluggable backends, resolution order per secret:

```yaml
# config.yaml
secrets:
  backend: auto          # auto | file | keychain | env
```

- `file` — today's `~/.jigga/secrets/<name>` (stays the default and the
  fallback; zero migration).
- `keychain` — OS keychain via the platform's **CLI**, no Python deps:
  Linux `secret-tool` (Secret Service/DBus), macOS `security
  add/find-generic-password`. `auto` = keychain when the CLI exists AND a
  session bus is reachable (headless servers fall back to file — DBus is
  usually absent), else file.
- `env` — explicit opt-in passthrough for CI/dev.

**API.** `get_secret(home, name)`, `set_secret`, `delete_secret`,
`list_secrets` (names only — values never enumerate). Existing modules
(`telegram.py`, `email_imap.py`, `web.py`, `chatgpt_auth.py`) migrate their
direct file reads to `get_secret`; their current files keep working via the
file backend.

**Agent-side gate (fixes the risk-register hole).** `secrets_required` in a
manifest stops being sufficient: the broker only releases a secret to a
capability invocation when the **executing agent** has
`permissions.secrets.allow` for that name (the evaluator already exists in
`evaluate_capability_permissions`; today it gates planning, not the actual
read — E1 makes the read itself go through it).

**CLI.** `jigga secrets set <name>` (value prompted, never argv), `list`,
`delete`, `migrate --to keychain`. `doctor` learns a backend check.

## E2 — OS sandbox backend

**Backend choice: `bwrap` (bubblewrap) on Linux; `sandbox-exec` on macOS
later.** Rationale: bwrap is unprivileged (no setuid config like firejail
profiles), composable as a plain argv prefix — which is exactly the
`run_sandboxed` seam — and packaged everywhere (it's what Flatpak uses).

**Mechanics.** `run_sandboxed(spec)` gains:

```yaml
sandbox:
  backend: auto          # auto | none | bwrap
```

When active, the subprocess argv is prefixed with a generated
`bwrap` invocation derived from the existing `SandboxSpec`:

- `--ro-bind` each `fs_read` path, `--bind` each `fs_write` path, tmpfs `/tmp`,
  proc/dev minimal; **nothing else mounted** — deny-by-default filesystem.
- `--unshare-net` when the spec declares no network need; otherwise network
  stays shared in E2 and gets constrained by E3.
- `--clearenv` + `--setenv` from the already-computed restricted env (the
  env-scrub logic is reused, now enforced by the kernel not by dict filtering).
- `--die-with-parent`, existing timeout unchanged.

**Rollout.** `auto` = off unless `bwrap` exists; first release ships
default-**off** (`none`) with `doctor` advertising it; flip `auto` on once the
prod deployment has run it warm for a week. Per-spec opt-out stays possible
(`sandbox: false` on a capability manifest, surfaced by the scanner as a
warning). `safe_process.py` (shell.run) converges onto `run_sandboxed` so
shell commands inherit the same bounds. Tests follow the no-real-system rule:
argv-construction is unit-tested; an opt-in integration marker exercises real
bwrap only when present.

## E3 — Per-capability egress allowlist

The gap E2 leaves: a subprocess with network access can reach *anywhere*.
Two-layer answer, cheapest-first:

1. **Deny-all is free:** capabilities that declare no network get
   `--unshare-net` in E2. Most packs (filesystem, skills, summarization)
   drop to zero egress immediately.
2. **Allowlisted egress via a local filtering proxy.** A small stdlib
   HTTP/HTTPS CONNECT proxy (`runtime/egress_proxy.py`) listens on localhost
   (per-invocation port, torn down with the subprocess); the sandboxed
   process gets `HTTP_PROXY`/`HTTPS_PROXY` pointed at it **and**
   `--unshare-net` is NOT used, but the proxy only CONNECTs to hosts in the
   capability's declared `permissions.network.allow`. Non-HTTP protocols are
   simply not proxied — combined with a default-deny firewall stance this
   covers the realistic exfil paths for the tools we run (HTTP APIs).
   DNS/iptables enforcement is explicitly deferred (needs privileges JIGGA
   shouldn't hold); the proxy is honest-about-bounds and audited
   (`egress.allowed` / `egress.blocked` events, which also gives us the first
   real *observed-behavior vs declared-manifest* signal the scanner can use).

Browser automation (the roadmap's "highest-blast-radius missing piece")
unblocks only after E2+E3 are default-on.

## Sequencing (each its own PR)

| Slice | Contents | Size |
|---|---|---|
| E1a | broker module + file/env backends + `jigga secrets` CLI + migrate existing readers | M |
| E1b | keychain backend (secret-tool/security CLIs) + `auto` + doctor check | S |
| E1c | broker-enforced agent secret grants (close the risk-register hole) | S |
| E2a | bwrap argv builder behind `run_sandboxed` + config + doctor + unit tests | M |
| E2b | converge `safe_process` onto the seam; `--unshare-net` for no-network specs; scanner warning for `sandbox: false` | S |
| E2c | flip `sandbox.backend: auto` default after prod soak | XS |
| E3a | egress proxy + wiring + audit events | M |
| E3b | scanner/doctor integration (declared vs observed egress) | S |

## Decisions to confirm before E1a

1. **Keychain on the prod server:** headless Linux almost certainly lacks
   Secret Service → prod stays on the `file` backend. Acceptable? (The broker
   still centralizes reads + enforces agent grants; at-rest encryption on a
   server needs disk encryption, not a keychain.)
2. **Passphrase-encrypted file backend** (age-style, like backup's approach —
   shell out, no Python crypto) as a middle option: worth a slice, or defer?
3. **bwrap availability in prod** (`apt install bubblewrap`) — install now so
   E2a can soak-test against reality?

## Risks

- DBus/keychain flakiness on servers → `auto` must degrade silently to file.
- bwrap inside containers/nested namespaces can fail (`--unshare-user`
  restrictions) → `auto` probes with a canary invocation at doctor-time.
- The CONNECT proxy sees hostnames only (TLS) — good enough for allowlisting,
  never claim it inspects content.
- MCP servers that legitimately need broad egress (a web-crawling MCP) will
  need explicit wide allowlists — the approval flow already surfaces that.
