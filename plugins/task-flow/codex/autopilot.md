# Autopilot in Codex

Follow the shared [Autopilot procedure](../skills/workflow/orchestration.md#autopilot--task-flowtask-autopilot)
for the run file, stack validation, overview, budgets, breaker and report.
Use the Task Flow root and configured policy from [Running Task Flow](run.md).
Write `run.md` through `autopilot.py write-run <run-dir> --expected-sha256
<hash-or-missing> --input <draft>`. Hash the original bytes before preparing
the draft. If the file changed, reread it and recalculate the update.

- Resolve external roles as in the single-task procedure. Use the configured
  external backlog profile with `role_run.py --role backlog` for decomposition
  and repository gathering. Refuse an unsupported role before dispatch.
  Resolve task-specific implementer routing before using its results.
- When creating tasks, use `phase_gate.py initialize`. For explicit existing
  tasks, preserve base and approval. If recorded bases differ from the
  proposed stack base, report the mismatch before creating the run. Apply a
  dependency's new base only with the gate's controlled rebase at dispatch.
- Plan with the single-task procedure through `planned`, preserving existing
  valid plans. Show the shared overview and obtain the run's start decision.
  For every task, use policy approval when allowed, or use
  [operator approval](approval.md). A batch start decision alone cannot
  authorize an individual task plan. Keep the run at `ready` until the
  required approvals have been recorded.
- Use `autopilot.py next` exactly as in the shared procedure. For each
  authorized task use the Codex single-task steps, then [local finish](finish.md)
  on `review-approved`. Worktree retention and the local commit let descendants
  use the recorded branch as their base. Do not push or open PRs in the batch.
- Follow the shared pause and stop rules. If a worker or finish fails, persist
  the pause reason and resume that task without redoing completed tasks.
  Recompute external usage from stored artifacts using the shared procedure.
