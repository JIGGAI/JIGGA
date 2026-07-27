# Web Capability Runtime Notes (2026-07-27)

`web.fetch` + `web.search` — the first open-web read surface (`runtime/web.py`,
bundled, stdlib-only). Fetch is **default-deny twice over**: the host must be in
`config.yaml` `web.allowed_domains` (exact or `*.wildcard`; empty = refuse with
instructions) AND pass the executing agent's network policy
(`evaluate_network`). Search uses the DuckDuckGo lite HTML endpoint (no API
key); its host is implicitly allowed for search only, and parsing is
best-effort — an empty parse returns an explicit `note`, never fabricated
results.

Responses are text-extracted (HTML tags stripped), JSON pretty-printed, and
truncated (`max_chars`, default 12k; 1.5MB download cap). Fetched content is
**untrusted, prompt-injectable input** — the capability is `risk_level: medium`
so calls are approval-gated outside `autonomous` mode.

Enable:

```yaml
# config.yaml
web:
  allowed_domains: ["docs.python.org", "*.wikipedia.org"]
```

Follow-ups: per-capability egress moves to the OS layer with Milestone E;
a robots/politeness pass and per-domain rate limiting if usage grows; browser
automation (JS rendering) stays blocked behind the Milestone E sandbox.
