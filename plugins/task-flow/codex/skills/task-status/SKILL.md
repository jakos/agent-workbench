---
name: task-status
description: Show the current Task Flow phase, approval, worktree and latest check or review for a task.
---

Use the exact task ID supplied by the user. Task IDs begin with a lowercase
letter or digit and contain only lowercase letters, digits and hyphens. If
the ID is missing, use the current conversation's explicit task ID or list
tasks and ask which one they mean.
Read `.work/<id>/status.md` in the project root and show its phase, round,
approval, branch, worktree and most recent History entry. If present, summarize
the check and review artifacts for that round. Do not change any task files.
