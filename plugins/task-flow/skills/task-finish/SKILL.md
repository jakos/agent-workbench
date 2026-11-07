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
No reassessment, risk-based stop, new approval gate or publication action is added.

Consult orchestration → **Policy** first: `scripts/policy.py` clamps `--auto`,
approval, round limits, finish actions, mode and budgets; no file keeps legacy behaviour.

When configured, run `scripts/phase_gate.py finish --task-dir <absolute-task-dir>`
through uv **before any finish side effect**. Require current plan authorization,
baseline/check evidence and a current APPROVED review covering every AC without
introduced check failures. Refusal leaves finish unstarted; repair and resume.
Use the gate's transition to done, retain gates.jsonl during pruning, and export only
its deterministic SHA-256 digest in enabled task records. No records opt-in is needed
to preserve the original gate evidence.
