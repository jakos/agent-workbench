---
name: task-list
description: List open task-flow tasks under .work/ sorted by what to do next; --all includes done tasks, --epic <id> lists only the member tasks of that epic.
argument-hint: "[--all] [--epic <epic-id>]"
disable-model-invocation: true
---

From the repository root, run:

```
uv run --quiet --script "${CLAUDE_PLUGIN_ROOT}/scripts/task_list.py" $ARGUMENTS
```

Show its output verbatim. Do not read any `status.md` file, do not re-sort, summarise or
add rows, and change nothing. If the script exits non-zero, show its stderr. Epics
(`.work/_epics/<epic-id>/`) are never listed as tasks; the Epic column shows a task's
epic, and `--epic <epic-id>` filters to that epic's members.

When the listing repository enables `.claude/task-flow.risk.toml`, the table adds a
risk column from only the status round's review: low, medium, high, unavailable
(missing review or legacy section), or invalid (malformed section). Earlier or future
reviews never substitute. Disabled reporting keeps the original table and ordering.
