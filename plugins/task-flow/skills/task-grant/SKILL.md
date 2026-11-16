---
name: task-grant
description: Consume the user's fresh finite extra-round grant for one task.
argument-hint: "<task-id> <ceiling>"
disable-model-invocation: true
---

Read `${CLAUDE_PLUGIN_ROOT}/skills/workflow/orchestration.md` and `${CLAUDE_PLUGIN_ROOT}/skills/workflow/SKILL.md` first.

The user types exactly `/task-flow:task-grant <task-id> <ceiling>`. Require the explicit
ordinary task id and a finite integer ceiling above the current limit. The ceiling is
the maximum round number, not a number of additional rounds. The real UserPromptSubmit
hook issues evidence for only that action, ceiling, current plan and resolved attempt,
single-use and valid for five minutes. These instructions create no authority.

Consume it with:

```text
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/phase_gate.py" grant --source user --ceiling <ceiling> --task-dir <absolute-task-dir>
```

Report the result. A changed plan or attempt, expiry, a different ceiling, or replay
refuses. The user must review the current plan and type the command again. Never
write evidence, invoke the issuance hook, or use `--terminal` through model tools.
Grants do not bypass no progress or circuit breakers and do not reset the attempt.
Resume a round-cap escalation with the ordinary implementing transition.

Explicit fallback: the user runs the same command with `--terminal` in a separate
interactive terminal and confirms the displayed digest, attempt, action and ceiling.
Bare consumer calls never issue evidence or open a challenge. With no policy
configuration the existing legacy round procedure still applies.
