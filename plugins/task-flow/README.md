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

Both actions retain an explicit `--terminal` fallback through `phase_gate.py`
in a separate interactive terminal; see the
[authorization procedure](skills/workflow/orchestration.md#enforced-transitions-when-policy-is-configured)
for full commands and recovery. Bare consumer calls never issue authorization.
