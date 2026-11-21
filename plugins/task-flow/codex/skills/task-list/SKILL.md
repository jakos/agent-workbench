---
name: task-list
description: List Task Flow tasks, including phase, round, and last review verdict.
---

From the project repository root, run:

```text
uv run --quiet --script "<task-flow-root>/scripts/task_list.py"
```

Resolve the Task Flow root from this skill's absolute path by walking
up from its directory through `skills` and `codex` to the plugin root. Pass
`--all` to include completed tasks and `--epic <id>` to filter by epic as
requested. Show the resulting table.
