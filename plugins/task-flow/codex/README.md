# Task Flow setup

Requires Codex CLI, Git, uv, and a configured model profile for each of
the planner, implementer, and reviewer roles. The agent-workbench checkout
already includes Codex profiles that use the ChatGPT login from `codex login`.

## This checkout

Open a new Codex session in the trusted agent-workbench checkout. Its
project skills use the skill instructions and Python scripts in your working
tree, including local edits. The project config disables the marketplace copy
here. Use `$task-run`, `$task-approve`, `$task-finish`, `$task-autopilot`, and
`$task-release` for the workflow and `$task-list` or `$task-status` to inspect it.

For Claude Code, start the development session from the checkout root:

```text
claude --plugin-dir ./plugins
```

This loads the checkout's Task Flow and dev-baseline for that session.
After editing Claude skills or hook definitions, use
`/reload-plugins` or start a new development session.

## Other repositories

Install from a GitHub release tag that includes the Codex integration.
Tag `task-flow-0.18.0` predates it. In the commands below, replace
`<release-tag>` with a published Task Flow tag that includes the Codex manifest.
On Windows PowerShell, use `codex.cmd` if script execution policy blocks the
npm PowerShell shim.

```text
codex plugin marketplace add jakos/agent-workbench --ref <release-tag>
codex plugin add task-flow@agent-workbench
```

If you previously registered the checkout with `codex plugin marketplace add .`,
replace that marketplace registration with the GitHub source before installing.
The project skills above load directly from the checkout on their own.

Open a new Codex session in your target repository. Type `$` and select
the Task Flow skill, then enter its arguments.

| Skill | Example input |
| --- | --- |
| `$task-flow:task-list` | `--epic wave-9-dead-ends-core` |
| `$task-flow:task-status` | `hook-command-precision` |
| `$task-flow:task-run` | `Add a unit test for the task-list sort order` |
| `$task-flow:task-run` | `<approved-task-id> --implementer codex-impl --reviewer codex-review` |
| `$task-flow:task-approve` | `<task-id>` to prepare a request, then the task ID and signed proof paths |
| `$task-flow:task-finish` | `<review-approved-task-id>` |
| `$task-flow:task-autopilot` | `--tasks <task-a>,<task-b>` or an existing run ID |
| `$task-flow:task-release` | `--target task-flow --patch --dry-run` |

Select a repository policy and external model profiles
using the shipped [policy](../templates/policy.toml) and
[model](../templates/models.toml) templates. Use the script checker and
Git worktrees. The Codex and Claude Code installations use the same task
files and project configuration.

## Approval and finish

A plan requiring human approval can use [signed operator approval](approval.md)
or the existing Claude prompt hook. After approval is recorded, invoke
`task-run` again with the same ID. Plans eligible for policy approval proceed
immediately. `task-run` stops at an approved independent review. Use
`task-run --finish` or `task-finish` for a local commit and task record.
Autopilot finishes locally
before dispatching a dependent task. Release and publication have separate gates.
