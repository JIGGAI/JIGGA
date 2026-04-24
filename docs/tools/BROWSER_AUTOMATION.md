# Browser Automation

## Purpose

Some work requires websites: research, form checks, dashboards, social media drafting, or QA. Browser automation gives agents a controlled way to inspect and interact with web pages.

## Product Definition

The **Browser Automation Tool** provides page navigation, reading, screenshotting, extraction, and limited interaction under a policy boundary.

## Core Tools

```yaml
tools:
  - browser_open
  - browser_read
  - browser_click
  - browser_type
  - browser_screenshot
  - browser_extract
```

## Tool Example

```yaml
tool: browser_open
input:
  url: "https://example.com"
  profile: isolated
  network_policy: allow
```

## Profiles

- `isolated`: no user cookies; safest default
- `user_readonly`: user profile/cookies, no form submission
- `user_interactive`: user profile/cookies, requires approval for actions

## Policy

```yaml
browser:
  default_profile: isolated
  allow_domains:
    - github.com
    - docs.example.com
  deny_domains:
    - banking.example.com
  require_approval_for:
    - form_submit
    - purchase
    - login
    - post_public_content
```

## Safety Rules

- Treat web content as untrusted and prompt-injectable.
- Do not let web pages instruct the agent to reveal memory or credentials.
- Require approval before posting, submitting, purchasing, deleting, or messaging.
- Prefer isolated profile for research.

## V1 Build Tasks

- Implement headless browser open/read/screenshot.
- Implement domain allow/deny.
- Add approval gate for side-effecting actions.
- Add extraction into structured notes.
