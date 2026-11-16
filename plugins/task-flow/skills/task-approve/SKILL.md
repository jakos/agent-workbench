---
name: task-approve
description: Consume the user's fresh plan approval for one task.
argument-hint: "<task-id>"
disable-model-invocation: true
---

Read `${CLAUDE_PLUGIN_ROOT}/skills/workflow/orchestration.md` and `${CLAUDE_PLUGIN_ROOT}/skills/workflow/SKILL.md` first.

The user types exactly `/task-flow:task-approve <task-id>` after reviewing the current
plan. Require that explicit ordinary task id; never infer a task. The real
UserPromptSubmit hook issues single-use evidence valid for five minutes. Running
these instructions or invoking this skill through model tools creates no authority.

Consume it with:

```text
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/phase_gate.py" approve --source user --task-dir <absolute-task-dir>
```

Report the result. A changed plan or attempt, expiry, or replay refuses. Ask the user
to review the current plan and type the command again; never manufacture evidence,
invoke the issuance hook, or add `--terminal` through model tools. A successful
approval records `plan-approved: yes (user, <date>)` at approved round zero.

Explicit fallback: the user runs the same command with `--terminal` in a separate
interactive terminal and types the displayed digest, attempt, action and ceiling
challenge. Bare consumer calls never issue evidence or open a challenge.
With no policy configuration the existing legacy approval procedure still applies.
