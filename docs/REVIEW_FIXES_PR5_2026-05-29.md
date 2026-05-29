# Review Fixes — PR #5 (2026-05-29)

Follow-up PR after #2/#3/#4 cleared review and merged. Picks up:

1. **Subagent deny-overlap polarity bug** discovered while reviewing PR #4's fix commit (`9631291`). The check rejects subagent deny rules that voluntarily narrow scope below the parent — the opposite of the spec's "subagents stricter than parents" rule.
2. **content-drafting risk_level surprise** — bundled at `medium` so the demo workflow `social_content_syndication` blocks under default `ask` mode.
3. **Memory/runtime context separation** in dispatcher (PR #4 nit #4).
4. **Broader capability permission enforcement** — beyond filesystem (PR #3 follow-up #1).
5. **Extensible handler dispatch** — manifest `handler` accepts dotted import paths (PR #3 follow-up #4).
6. **First-use approval records** for user-local capability packs (PR #3 follow-up #2).
7. **Manifest security scanner** — dangerous patterns + remote-script flagging (PR #3 follow-up #3).

Status legend: ✅ done · 🟡 partial · ⏭️ deferred.

## Changes

(filled in as work proceeds)
