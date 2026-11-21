# Running Task Flow in Codex

Use the project Git root as the command working directory. Resolve the
Task Flow root from this file's parent directory, `codex/`, then
one level up. In commands below, `run SCRIPT ARGS` means
`uv run --quiet --script "<task-flow-root>/scripts/SCRIPT" ARGS`.
Use absolute paths for the project, task directory and worktree.

Use the [shared workflow contract](../skills/workflow/SKILL.md) for file
formats. The [shared orchestration guide](../skills/workflow/orchestration.md)
describes the phases and artifact conventions. Use the Codex procedure here
for role dispatch and approval. Keep existing project rules and verification
commands from `.claude/task-flow.md`.

## Start or plan

1. Run `policy.py show --project <project> --format json`. This entry point
   advances tasks under configured policy. If the result is legacy, show how
   to opt into repository policy using the shipped `templates/policy.toml`.
   Respect policy errors and required isolation.
2. For a new task, use the shared guide's Phase new to choose a unique ID and
   write `task.md` and the workspace ignore file. To create the status, run
   `phase_gate.py initialize --task-dir <task> --base <current-branch>`.
   For an existing task read its status and resume from its recorded phase.
   Use the existing base and task branch. If the task belongs to a stacked
   run that needs a base change, use the shared guide's controlled rebase
   procedure before allocating a worktree. Honor a paused Autopilot run and
   any dispatch refusal from the gate.
3. Resolve the planner when planning and the reviewer before dispatch with
   `models.py select <role> --project <project>`, adding
   `--override <profile>` when requested. In agent-workbench, if either
   role selects a Claude subagent, use the shipped project profiles
   `codex-plan` and `codex-review` respectively if enabled. In other
   projects use a configured external profile for each producer role. A
   CLI planner or reviewer needs its configured read-only command.
4. In phase `new`, run
   `role_run.py --role planner --task-dir <task> --project <project> --worktree none --profile <planner>`.
   Exit 3 means a validated blocked plan. Show its questions and keep the
   task at `new`. Other nonzero exits are resumable refusals. On success,
   run `phase_gate.py transition --task-dir <task> --to planned` and
   present the plan's criteria and verification commands.
5. In phase `planned`, query
   `policy.py decide plan-approval --project <project> --plan <task>/plan.md`.
   Exit 0 allows `phase_gate.py approve --task-dir <task> --source policy`.
   Exit 2 is a policy error to report. On exit 1, show the plan and prepare a
   [signed operator approval request](approval.md).
   Resume this skill with the same task ID once approval is recorded.
   Preserve existing valid plans and approvals on resume.

## Run approved work

1. Resolve `models.py roles --project <project> --task-dir <task>`.
   Use the selected external implementer profile. An explicit
   `--implementer` may choose an enabled external profile when routing
   is inactive. In agent-workbench, if the selection is a Claude subagent
   and routing is inactive, use the configured `codex-impl` profile.
   Use `gates.py` when the checker selection is `script`. Report unsupported
   role selections before dispatch.
2. Check the project rules for `worktree: no` and whether the task already
   ran in place. Hand those tasks back to the Claude workflow for in-place
   execution. Before creating a worktree for an approved task, transition to
   `baseline` at round zero, recording `--worktree .work/<id>/worktree`.
   This validates the current approval before the Git operation. Create the
   worktree using `git worktree add -b task/<id> <absolute-worktree> <base>`.
   If the worktree already exists, verify it is registered to the project
   and on the expected task branch before reusing it. If creation fails,
   retain phase `baseline` and the reserved path for resumption.
3. In phase `baseline`, validate or create the recorded worktree as above.
   Run `gates.py --task-dir <task> --worktree <worktree> --round 0`.
   Once baseline evidence exists, follow the shared guide's routing
   preparation if routing is active. Enter `implementing` with `--round 1`.
4. In `implementing`, run
   `implement.py --task-dir <task> --worktree <worktree> --round <n> --profile <implementer>`.
   On exit 3 transition to `blocked` and report its recorded reason. On
   exit 0 transition to `checking`. Other nonzero exits leave the round
   resumable in `implementing`.
5. In `checking`, run
   `gates.py --task-dir <task> --worktree <worktree> --round <n>`.
   Follow the shared guide's external review procedure using
   `role_run.py --role reviewer --task-dir <task> --project <project> --worktree <worktree> --round <n> --profile <reviewer>`.
   Transition to `reviewing` once a valid review is published. If resuming
   from `reviewing`, use the current-round review or run the reviewer if
   that artifact is missing. Failed script invocations leave the phase
   resumable.
6. Follow the shared guide's loop and stop rules. Use
   `phase_gate.py transition --task-dir <task> --to <phase>` for every
   transition. On APPROVED, transition to `review-approved` and report
   the result, worktree and review path. For CHANGES_REQUESTED, the gate
   decides whether to enter the next implementation round or escalate
   for the round cap or no progress. Surface other gate refusals and keep
   the current phase. Use [local finish](finish.md) when the user requests to
   finalize the task. Batch execution calls finish after an approved review.

If a script runs longer than the foreground tool limit, wait for its existing
process and read the final exit code before advancing. A subsequent invocation
can resume by task ID. Keep task state changes inside the existing Python
commands.
