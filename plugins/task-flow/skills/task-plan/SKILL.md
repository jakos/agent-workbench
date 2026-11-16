---
name: task-plan
description: Plan a task-flow task with the planner agent, then ask the user to approve, edit or cancel the plan.
argument-hint: "[task-id] [--planner <profile>]"
disable-model-invocation: true
---

Read `${CLAUDE_PLUGIN_ROOT}/skills/workflow/orchestration.md` and `${CLAUDE_PLUGIN_ROOT}/skills/workflow/SKILL.md` first. They are the
procedure; this command only says which part to run.

Follow **Phase: plan** for task `$ARGUMENTS`, including the approval gate. Never record approval the user did not give.

When policy requires human approval, present the plan and have the user type
`/task-flow:task-approve <task-id>`. Its real prompt hook issues evidence for the
current plan and attempt, valid for five minutes and one use. Follow task-approve
to consume it. Changed plans and expiry require a fresh user command. An explicit
`approve --source user --terminal` fallback remains available to the user in a
separate interactive terminal; never run that fallback through model tools.

- `--planner <profile>` plans with that profile for this invocation only, instead of the
  configured `[roles] planner`; it must name an enabled configured profile of
  models.toml — never a free-form model or command (see **Phase: plan** step 0).

Consult orchestration → **Policy** first: `scripts/policy.py` clamps `--auto`,
approval, round limits, finish actions, mode and budgets; no file keeps legacy behaviour.
