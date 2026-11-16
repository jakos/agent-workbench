---
name: task-run
description: Run a task end to end with task-flow: new, plan, approval gate, a baseline check, then up to 3 rounds of implement, check and review.
argument-hint: "<description | task-id> [--auto] [--planner <profile>]"
disable-model-invocation: true
---

Read `${CLAUDE_PLUGIN_ROOT}/skills/workflow/orchestration.md` and `${CLAUDE_PLUGIN_ROOT}/skills/workflow/SKILL.md` first. They are the
procedure; this command only says which part to run.

Arguments: `$ARGUMENTS`

- If the first argument names an existing `.work/<id>/`, resume that task from the phase in its `status.md`.
- Otherwise it is a new request: follow **Phase: new** with everything except `--auto`.
- `--auto` skips only the plan approval question, and only when the user typed it. It never permits an early stop, push, PR or deploy.
- `--planner <profile>` plans a new task (or a task still in phase `new`) with that profile for this invocation only, instead of the configured `[roles] planner`; it must name an enabled configured profile of models.toml. On resume without the flag, the current configuration applies — history is evidence, not a sticky selection — and an existing valid `plan.md` of a `planned`/`approved` task is never silently replanned.

Then follow **The loop**. Update `status.md` after every phase. Stop only at: the approval gate (unless `--auto`), `BLOCKED` from the planner or implementer, `review-approved`, no progress (escalation), or the round cap (escalation after round 3). Never stop on your own judgment that the run cannot pass. A failed external implementer round (exit 1 or 2 of `implement.py`) is not a stop: the same command resumes that round.

Consult orchestration → **Policy** first: `scripts/policy.py` clamps `--auto`,
approval, round limits, finish actions, mode and budgets; no file keeps legacy behaviour.

When configured, follow **Enforced transitions when policy is configured**:
`scripts/phase_gate.py` owns protected status transitions. Use the recorded,
plan-bound approval, complete baseline evidence before either implementer route and
include `Task directory: <absolute-task-dir>` in Claude launch prompts. Never infer
human approval from an automatic prompt turn. Failed external invocations resume in
the same round. The user can authorize the current plan with
`/task-flow:task-approve <task-id>` and grant a finite maximum round with
`/task-flow:task-grant <task-id> <ceiling>`. Consume the real hook's evidence within
five minutes using the corresponding skill. Changed plans, attempts, expiry and
replay require a fresh user command. The explicit `--terminal` fallback for both
actions is user-only in a separate interactive terminal. A grant cannot bypass
no progress or circuit breakers.
