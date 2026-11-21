---
name: task-run
description: Start or resume a single Task Flow task in Codex using an external planner, implementer and independent reviewer.
---

Read [Running Task Flow in Codex](../../run.md) before starting. A task ID
resumes that task. A description starts a new task. The user can name
`--planner <profile>`, `--implementer <profile>` and `--reviewer <profile>`
from the project's existing Task Flow profile configuration. With
`--finish`, finish the reviewed task locally after the loop.

Use the Task Flow scripts from the same copy as the guide to validate
artifacts and advance phases.
Stop at a required human approval and resume only after it has been recorded
by Task Flow. On an approved independent review, report the worktree and
task state. With `--finish`, follow [local finish](../../finish.md) before
reporting.
