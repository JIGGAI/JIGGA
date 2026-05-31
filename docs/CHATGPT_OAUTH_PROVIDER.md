# ChatGPT-subscription model provider (`chatgpt_oauth`)

Run JIGGA agents on a **ChatGPT Plus/Pro subscription** instead of a billed API key — flat-rate, no per-token charges. This mirrors how the Codex CLI and openclaw work: hold an OAuth access token for `chatgpt.com/backend-api` rather than an `OPENAI_API_KEY`.

## How it works

`model_router` gains a provider kind `chatgpt_oauth`. On each call it:

1. **Loads credentials** (`runtime/chatgpt_auth.py`). JIGGA keeps its **own** store at `<home>/secrets/chatgpt_auth.json`, written by its native login (below). When that's absent it falls back to importing the token the **Codex CLI** stores at `~/.codex/auth.json`, so an existing `codex login` just works.
2. **Refreshes when expired** — decodes the access-token JWT `exp`, and if stale POSTs `grant_type=refresh_token` to `auth.openai.com/oauth/token` (client id `app_EMoamEEZ73f0CkXaXp7hrann`), persisting the rotated tokens back so codex and JIGGA stay in sync. A live 401/403 also triggers one refresh-and-retry.
3. **Calls the Responses API** — `POST https://chatgpt.com/backend-api/codex/responses` with `Authorization: Bearer <access>`, `chatgpt-account-id`, `OpenAI-Beta: responses=experimental`, and the Codex `originator`/`User-Agent`. The request is the Responses shape (`instructions` + `input` items + flattened `tools`, `store:false`, streamed).
4. **Parses the SSE stream** — text and tool calls arrive on `response.output_item.done` items (this backend leaves `response.completed.output` empty); token usage comes from `response.completed`.

JIGGA's agent tool-use loop, cost tracking, budgets, trace, and audit all work unchanged on top — `model.call` events record token usage as normal.

## Config

```yaml
models:
  defaults: {provider: chatgpt}
  providers:
    chatgpt:
      kind: chatgpt_oauth
      default_model: gpt-5.5      # only model a ChatGPT account is served here
      timeout_seconds: 120
  profiles:
    default: {primary: chatgpt, fallback: []}
  pricing:
    gpt-5.5: {input_per_1k: 0.0, output_per_1k: 0.0}   # subscription: $0 marginal; tokens still tracked
```

## Onboarding & login (no codex dependency)

JIGGA authenticates itself — the Codex CLI is only a fallback credential source, not a requirement.

```bash
jigga model setup            # interactive: pick provider, then authenticate
# or step by step:
jigga model use chatgpt      # make ChatGPT the default provider (writes config.yaml)
jigga model login            # browser flow: open a URL, paste the return URL back
jigga model login --device-code   # headless: enter a short code at auth.openai.com/codex/device
jigga model status           # show provider + login state (jigga store vs codex fallback)
```

- **Browser-paste flow** (`chatgpt_login.browser_login`): JIGGA prints the PKCE `auth.openai.com/oauth/authorize` URL; you sign in; OpenAI redirects to `localhost:1455/auth/callback?code=…` (which won't load — that's fine); you paste that return URL back; JIGGA verifies `state`, exchanges the code, and writes tokens to its own store.
- **Device-code flow** (`chatgpt_login.device_login`): requests a user code from `auth.openai.com/api/accounts/deviceauth`, shows it + `auth.openai.com/codex/device`, polls until you approve, then exchanges the server-issued code. Best for headless/remote installs.

Tokens land in `<home>/secrets/chatgpt_auth.json` (0600) and are refreshed automatically.

## Constraints & notes

- **Model:** a ChatGPT account is only served **`gpt-5.5`** on this endpoint; `gpt-*-codex` slugs are rejected with HTTP 400.
- **Cost:** there is no per-token dollar cost on a subscription, so pricing is `0` — but token counts are still recorded, which is what you watch against the subscription's rolling usage quota (`jigga cost`).
- Verified against codex `0.135.0` and the live backend (client id, endpoints, headers, response + device-code request shapes confirmed against the real service, not assumed).
