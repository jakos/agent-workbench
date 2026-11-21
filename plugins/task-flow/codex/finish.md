# Finish a task in Codex

Use the installed or checkout Task Flow root for all scripts. Run
`phase_gate.py finish --task-dir <task>` before any side effects. Require
configured policy, phase `review-approved`, and a worktree registered to the
repository on the recorded task branch. Refuse detached, missing, ambiguous, or
in-place checkouts. Read the project's Finish rules before committing. If they
disable commits, leave the review-approved task available for manual handoff.
For a phase `done` task, report the existing outcome.

Follow [Phase finish](../skills/workflow/orchestration.md#phase-finish--task-flowtask-finish)
for the optional SDLC refresh, summary with the validated review's risk, optional
task record and trailers, and local commit. Stage only reviewed changes and the
documented finish-generated files. Verify the staged diff against the review
before committing. Stop if the task branch has unexpected commits or the worktree
has unrelated changes. If a prior finish committed successfully, reuse that
commit only after verifying it matches the reviewed task and finish files.

If publication is enabled, follow the shared guide's trusted publication handoff
to prepare the token-free request. The operator runs the publisher separately.
Report local commit and publication status separately. Keep the worktree and
branch. Once local finish prerequisites are satisfied, run
`phase_gate.py transition --task-dir <task> --to done` and
`prune_task.py <task>`. Report the result of each command. Keep commits local
unless the user explicitly requests a separate delivery operation.
