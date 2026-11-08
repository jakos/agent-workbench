---
name: task-finish
description: Finish an approved task-flow task: write summary.md, propose commit message and PR text, commit locally, ask before push, PR or worktree removal, then prune .work/<id>/ to its Markdown files and impl-*.json metrics.
argument-hint: "[task-id]"
disable-model-invocation: true
---

Read `${CLAUDE_PLUGIN_ROOT}/skills/workflow/orchestration.md` and `${CLAUDE_PLUGIN_ROOT}/skills/workflow/SKILL.md` first. They are the
procedure; this command only says which part to run.

Follow **Phase: finish** for task `$ARGUMENTS`: the opt-in SDLC status refresh, the
opt-in task record and `Task-*` trailers (`scripts/task_record.py`, when the worktree's
`.claude/task-flow.records.toml` enables records), `summary.md` with its fixed
headings, the proposed commit message and PR text, the local commit, a separate
question before each step that leaves the machine (push, PR) and before removing the
worktree. The final prune of `.work/<id>/` to its `.md` and `impl-*.json` files is the
one deletion that is not asked.

When risk reporting applies at the recorded base, summary `## Goal` and the proposed
PR description open their substantive content with current-round risk, explanation
and attention list reused from the validated review. Keep fixed summary headings;
missing review means unavailable. Disabled reporting keeps existing instructions.
Without publication opt-in, no reassessment, risk-based stop, new approval gate or publication action is added.

Consult orchestration → **Policy** first: `scripts/policy.py` clamps `--auto`,
approval, round limits, finish actions, mode and budgets; no file keeps legacy behaviour.

When configured, run `scripts/phase_gate.py finish --task-dir <absolute-task-dir>`
through uv **before any finish side effect**. Require current plan authorization,
baseline/check evidence and a current APPROVED review covering every AC without
introduced check failures. Refusal leaves finish unstarted; repair and resume.
Use the gate's transition to done, retain gates.jsonl during pruning, and export only
its deterministic SHA-256 digest in enabled task records. No records opt-in is needed
to preserve the original gate evidence.


### Trusted publication handoff (project opt-in)

Without project `[publish] enabled = true`, use the existing finish procedure.
With opt-in, never inherit a `TASK_FLOW_PUBLISH_` variable into this session,
subagents, implement.py or role_run.py, including through `pass_env` or an API key
setting. A credential-bearing publisher runs only in a separate operator terminal
or service launched outside the model process tree. Command-denial hooks are not an
OS security boundary against arbitrary code as the same user.

The review transition records `publication-review-<n>.md` before reviewer launch.
Require this evidence, records enabled, current risk evidence, and all checks passing.
Run the existing finish gate before SDLC refresh, summary, deterministic record and
trailers. Commit once on the exact task branch, then run the token-free
`uv run --script plugins/task-flow/scripts/publish_task.py prepare --task-dir <absolute-task-dir>`.
It binds the request to the reviewed tree, permitted deterministic finish files,
attempt, plan, checks, review and final commit. Unexpected edits refuse the handoff.

Show `publication-request.md` and its digest. `push` and `pr` need separate effective
policy decisions; plan approval never authorizes either. For an ask decision the
human runs `grant-push` or `grant-pr` with the same `--task-dir` in a separate
interactive terminal and confirms the exact request digest. The orchestrator does
not invoke the credential-bearing `publish`, `advance` or `delivery` operations.
The external launcher runs those commands with process-scoped authentication only;
see the setup checklist in `docs/task-flow/policy.md`.

Retain every `publication*.md` through pruning. Keep task branches and worktrees
needed by descendants until delivery verification is complete. Report local
completion separately from publication receipts and verified delivery to `main`.
Children start draft against the recorded parent branch; the operator advances them
after parent delivery, waits for checks on the new base, then merges by hand with a
merge commit. Never automatically merge, force-rebase or delete a branch.

For publication opt-in, `publication-check-<n>.md` binds the source before checks;
the review transition rejects source changes made during or after those checks.
The transition to review-approved seals the review digest
and source snapshot in `publication-approval-<n>.md`. Subsequent review edits refuse
publication. Agent write tools cannot create or edit publication evidence or grants;
trusted workflow scripts own these retained Markdown files.
