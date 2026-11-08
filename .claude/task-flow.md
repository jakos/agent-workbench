# task-flow configuration

<!-- Project rules for the task-flow planner in THIS repository (agent-workbench).
     Format: plugins/task-flow/templates/task-flow.md. Plain Markdown: agents read it,
     policy overrides Finish push/pr when task-flow.policy.toml exists. -->

## Verification commands

<!-- One gate for every profile: this list replaces the defaults of python, docs,
     config and generic alike. It matches the checks of [targets.task-flow] in
     .claude/task-flow.release.toml entry by entry, in order, and scripts/verify.py
     refuses to run when the two lists disagree or when a discovered
     plugins/task-flow/tests/test_*.py is missing, duplicated or stale here and there —
     CI (.github/workflows/ci.yml) runs that script, so this
     list and the release list are one shared list, not two. There is no shell glob: a
     new plugins/task-flow/tests/test_*.py file is added here and there by hand. The
     Ruff commands pin the exact version (0.17.3) whose settings the committed ruff.toml
     records, so a clean runner matches a local run. -->

### python, docs, config, generic
```
uv run --script plugins/task-flow/tests/test_autopilot.py
uv run --script plugins/task-flow/tests/test_bench.py
uv run --script plugins/task-flow/tests/test_bench_publish.py
uv run --script plugins/task-flow/tests/test_docs.py
uv run --script plugins/task-flow/tests/test_doctor.py
uv run --script plugins/task-flow/tests/test_drivers.py
uv run --script plugins/task-flow/tests/test_gates.py
uv run --script plugins/task-flow/tests/test_guard.py
uv run --script plugins/task-flow/tests/test_implement.py
uv run --script plugins/task-flow/tests/test_launch.py
uv run --script plugins/task-flow/tests/test_models.py
uv run --script plugins/task-flow/tests/test_prune_task.py
uv run --script plugins/task-flow/tests/test_release.py
uv run --script plugins/task-flow/tests/test_rules.py
uv run --script plugins/task-flow/tests/test_task_list.py
uv run --script plugins/task-flow/tests/test_ci.py
uv run --script plugins/task-flow/tests/test_sdlc_status.py
uv run --script plugins/task-flow/tests/test_epics.py
uv run --script plugins/task-flow/tests/test_task_record.py
uv run --script plugins/task-flow/tests/test_cli_driver.py
uv run --script plugins/task-flow/tests/test_role_run.py
uv run --script plugins/task-flow/tests/test_role_contracts.py
uv run --script plugins/task-flow/tests/test_risk.py
uv run --script plugins/task-flow/tests/test_policy.py
uv run --script plugins/task-flow/tests/test_metrics.py
uvx ruff==0.17.3 check plugins/task-flow/scripts plugins/task-flow/tests plugins/task-flow/bench
uvx ruff==0.17.3 format --check plugins/task-flow/scripts plugins/task-flow/tests plugins/task-flow/bench
claude plugin validate .
uv run --script plugins/task-flow/tests/test_phase_gate.py
uv run --script plugins/task-flow/tests/test_publish_task.py
uv run --script plugins/task-flow/tests/test_pr_review.py
```

## Architecture rules

- Feature and fix tasks never change a version number: do not edit `version` in
  `plugins/*/.claude-plugin/plugin.json` or in `.claude-plugin/marketplace.json`, and
  do not write `CHANGELOG.md` entries. Versions and changelogs are set only by
  `/task-flow:task-release`, configured in `.claude/task-flow.release.toml`.

## Publication

Advisory PR review is independently opt-in through `.claude/task-flow.pr-review.toml`
and the trusted-base `REVIEW.md` policy. It remains disabled here. Setup, spending,
fork exclusions and the provider contract follow REVIEW.md. Human owners
retain merge decisions; AI review is not a required deterministic check.

Publication stays disabled here. An operator may opt in with the project-only
`[publish]` table in the project policy configuration. Keep the publication
credential outside every model session and runner. Local completion, PR publication
and delivery into `main` are separate outcomes; human merge commits preserve task
ancestry. Never push or change live GitHub settings during implementation or tests.
