---
name: task-release
description: Release one target (a plugin or project) in one controlled procedure — preconditions, version proposal, checks, changelog entry, one approval, version bump, release commit and annotated tag; push only after a separate question, default no.
argument-hint: "[<version> | --major | --minor | --patch] [--target <name>] [--since <ref>] [--dry-run]"
disable-model-invocation: true
---

Read `${CLAUDE_PLUGIN_ROOT}/skills/workflow/orchestration.md` and `${CLAUDE_PLUGIN_ROOT}/skills/workflow/SKILL.md` first. They are the
procedure; this command only says which part to run.

Follow **Release** for `$ARGUMENTS`. The release has exactly one approval gate and it is
skipped only when `policy.py decide release` returns `auto (explicit)`; otherwise
ask — there is no `--auto` for it. The push
gets its own question afterwards, default no.

Consult orchestration → **Policy** first: `scripts/policy.py` clamps `--auto`,
approval, round limits, finish actions, mode and budgets; no file keeps legacy behaviour.
