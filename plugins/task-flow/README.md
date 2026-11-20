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
