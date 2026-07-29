# Telemetry (opt-in)

**Off by default.** `jigga telemetry on` enables it; `off` disables; nothing
is collected or sent while disabled. `jigga telemetry report` prints the
**exact JSON** a send would transmit — before or after opting in.

## Documented payload (schema 1)

Counts only, never content: a locally-minted random `install_id`, JIGGA
version, OS/python version, config *shape* (number of agents/teams/workflows),
and 24h event/error **type counts** from an explicit allowlist of event-type
prefixes. Never sent: prompts, messages, agent names, file paths, event
details, secrets, or anything a human typed. Field-level truth is
`runtime/telemetry.py::build_payload` — the report command IS the payload.

Sent at most once/day from the supervisor heartbeat (contained; an
unreachable endpoint is a one-line audit event, never a fault).
`telemetry.endpoint` overrides the default collector — point it at your own,
or leave telemetry off entirely.
