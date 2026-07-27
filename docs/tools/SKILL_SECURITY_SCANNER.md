# Skill Security Scanner

## Purpose

A capability/skill ecosystem creates risk. Open-ended Markdown instructions plus scripts can trick agents or users into executing dangerous behavior. JIGGA should treat third-party packs as untrusted by default.

## Product Definition

The **Skill Security Scanner** analyzes capability packs before installation or enablement and produces a risk report.

## Scan Targets

- `CAPABILITY.md`
- `manifest.yaml`
- scripts
- package files
- install hooks
- external URLs
- binary dependencies
- symlinks

## Risk Signals

```yaml
risk_signals:
  - shell_access_requested
  - network_access_requested
  - secrets_path_access
  - ssh_path_access
  - browser_profile_access
  - crypto_wallet_path_access
  - curl_pipe_shell
  - obfuscated_code
  - post_install_hook
  - broad_filesystem_access
  - hidden_files
  - symlink_present
```

## Report Example

```yaml
security_report:
  capability: twitter-publisher
  risk: high
  findings:
    - severity: high
      reason: "Requests browser profile access"
    - severity: high
      reason: "Script downloads remote executable"
  recommendation: reject
```

## Install Flow

```text
User requests install
  ↓
Scan pack
  ↓
Show risk report
  ↓
Require approval or reject
  ↓
Install disabled by default
  ↓
User enables with explicit permissions
```

## Safety Rules

- Reject symlinks.
- Reject hidden executable payloads by default.
- Flag remote script downloads.
- Require approval for shell/network/email/calendar/browser capabilities.
- Prefer read-only mode for first run.
- Keep a provenance record.

## V1 Build Tasks

- Implement static scanner.
- Add manifest permission diff.
- Add symlink detection.
- Add dangerous command pattern detection.
- ✅ Shipped as `jigga capabilities validate <path>` (scanner runs on validate + install).
