# task-flow

A reviewed task workflow: plan, approve, implement, check, and review changes in isolated Git worktrees.

## Commands

```text
/task-flow:task-run "<request>"
/task-flow:task-approve <task-id>
/task-flow:task-grant <task-id> <ceiling>
/task-flow:task-status
/task-flow:task-finish
/task-flow:task-autopilot
/task-flow:task-doctor
```

Review the current plan, then type the exact approval command in Claude Code.
The real user-prompt hook issues evidence for one task, plan, attempt and action;
grants additionally bind a finite maximum round above the current limit. Evidence
expires after five minutes and is single-use. A changed plan, expiry or replay
requires a fresh user command. Invoking skill instructions creates no authority.
Grants cannot bypass no progress or circuit breakers.

If the prompt hook is unavailable, there is no human approval or grant fallback.
Existing `--auto`/policy approval may proceed only where policy permits; it never
grants extra rounds or bypasses stop rules.
See the [authorization procedure](skills/workflow/orchestration.md#enforced-transitions-when-policy-is-configured)
for full commands and recovery. Bare consumer calls never issue authorization.

## Claude subagent effort

`[roles.effort]` in models.toml sets the reasoning effort of each Claude subagent role
(`planner`, `implementer`, `checker`, `reviewer`, `backlog`). The levels are `low`,
`medium`, `high`, `xhigh` and `max`:

```toml
[roles]
planner = "claude:opus"

[roles.effort]
planner = "high"
checker = "low"
```

The orchestrator passes the level as the `effort` argument of every subagent launch of
that role; `models.py roles` and `models.py select` report it as `effort`. A role
without an entry has `effort: null` and keeps today's behaviour: the subagent's own
default applies. The table merges per role across the user and project files, and the
project file wins. Effort applies only to Claude roles: a role whose value is a profile
name or `script` (including `[roles.implementer]`) with an effort is a configuration
error, so set effort in the same file that makes the role a Claude role. The session's
`/effort` setting does not reach subagents, so use this table instead.

## OpenAI API billing

Any profile-capable role (planner, implementer, reviewer, backlog) can run OpenAI or
Codex models on per-token API billing instead of a subscription, through two paths.
`templates/models.toml` carries a commented example of each, and this repository's
`.claude/task-flow.models.toml` defines `openai-api` and `codex-api` as unused examples
(no role points at them).

- `openai-loop` against `https://api.openai.com/v1`: `billing = "api"`,
  `api_key_env = "OPENAI_API_KEY"`, `max_tokens_param = "max_completion_tokens"` and
  the `price_*` keys. The Bearer key is read from that variable at run time.
- Codex CLI (`driver = "cli"`): `billing = "api"`, `api_key_env = "CODEX_API_KEY"`
  (`codex exec` reads that variable instead of the saved `codex login`) and
  `usage_format = "codex-jsonl"` plus the `price_*` keys for a cost figure.

`api_key_env` is only the name of an environment variable; no key ever belongs in a
file. For a CLI profile the child environment is an allowlist, so `pass_env` must
contain the `api_key_env` name; models.py refuses the profile otherwise, because the CLI
would silently fall back to its saved login while task-flow accounted API cost. The
reverse is also a risk: a key named in `pass_env` of a `billing = "subscription"` profile
would be billed per token and still be accounted as unknown, so keep keys on API profiles.

Cost is the configured prices times the usage the provider reports (reasoning tokens are
part of the output count and are billed once). On `driver = "cli"` it is `null` (unknown,
never zero) when the CLI reports no usage or no `price_*` key is set. On `openai-loop`
there is no such guard: missing or all-zero `price_*` keys give a cost of 0 USD, so
`max_cost_usd` can never act (set the prices; `doctor` warns about it). A reply without
a `usage` object counts as zero tokens for the implementer (`implement.py`), but for the
planner, reviewer and backlog (`role_runtime.py`) it makes the run's cost `null`
(unknown), after which `max_cost_usd` can no longer act. `max_cost_usd` is enforced per
turn on `openai-loop`, and cumulative across the fallback chain. It is refused on
`driver = "cli"`: the CLI owns its loop and reports usage only when it ends, so the
bounds there are `timeout_seconds`, the per-run cost recorded in `impl-<n>.json` and
`role-*.json`, and the autopilot `External USD` accounting.

## Verification workers

`[gates] workers` in models.toml is a positive integer and defaults to 1 (serial).
The project file `.claude/task-flow.models.toml` overrides the user file
`~/.claude/task-flow/models.toml` key by key. For example:

```toml
[gates]
workers = 4
```

`scripts/gates.py`, `scripts/verify.py` and `scripts/release.py verify` use bounded
workers for independent commands. Dependent commands require `workers = 1`.
Reports retain input order, numbering and failure details even when checks finish
out of order; failures and ordinary worker exceptions do not omit later results.
Configuration, list validation, authorization and sandbox preparation finish before
submission, where applicable. Sandbox failures never fall back to host execution.

This repository keeps full shared-list parity: its project verification block and
release checks contain the same 30 commands in the same order, including pinned Ruff
and plugin validation. `scripts/verify.py` checks parity and test discovery before
executing the list. Concurrency changes scheduling, not coverage.
