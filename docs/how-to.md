# Using agent-workbench

## Prerequisites and baseline

Install Claude Code, Git and [uv](https://docs.astral.sh/uv/), then follow the
[installation commands](../README.md#install). Keep uv on the Claude Code process's
PATH. Python scripts declare their runtime and dependencies for uv to manage.
The formatter needs the project's Ruff or a global fallback:

```text
uv tool install ruff
```

Open Claude Code in the repository. Ask it to bootstrap the project, identify the
verification gate and write a concise CLAUDE.md. dev-baseline's project-bootstrap
skill guides this setup; python-tooling handles Python setup and formatting,
debug-systematically guides investigations, and commit-and-pr helps prepare changes.
Python edits are formatted automatically. For a separate code review, invoke:

```text
@code-reviewer
```

The [baseline walkthrough](../GETTING-STARTED.md) gives more detail.

## Check task-flow setup

Run in Claude Code from the repository:

```text
/task-flow:task-doctor
```

Each WARN or FAIL includes its fix. With configured external profiles and their API
keys available to the Claude Code process, `/task-flow:task-doctor --online` also
checks provider model IDs. It does not spend model tokens. Optional role profiles
live in `.claude/task-flow.models.toml` or `~/.claude/task-flow/models.toml`; start
with the [commented template](../plugins/task-flow/templates/models.toml).

## Start, approve and resume a task

For example, in Claude Code:

```text
/task-flow:task-run "Fix the parser's handling of empty input"
```

The planner writes a plan with steps, testable acceptance criteria and exact
verification commands. Review it, then approve, request edits or cancel. Files under
`.work/<task-id>/` carry the plan, status and evidence between fresh-context roles.
An approved plan first receives deterministic baseline checks on the untouched base
in round 0; pre-existing failures are recorded separately from introduced failures.

The implementer changes the planned files, the checker runs the verification
commands, and an independent reviewer judges every acceptance criterion against
the diff and check evidence. The bounded fix loop permits at most three rounds
(policy may lower this); later rounds address only the preceding review's Fix list.
A repeated nonempty failing set triggers no-progress escalation. A wrong plan is
reported as BLOCKED for a human decision; round-cap failures also return to a human.

To inspect or resume, substitute the actual ID in this illustrative example:

```text
/task-flow:task-status
/task-flow:task-run <task-id>
```

Resume uses the recorded phase. For separate phase commands, the same ID can be
passed to `/task-flow:task-check` (baseline at round 0) and `/task-flow:task-review`
(after current-round checks; never at round 0). Normally task-run drives the loop.

Automatic plan approval with `/task-flow:task-run <task-id> --auto` is
policy-controlled. `.claude/task-flow.policy.toml` can require approval, restrict
rounds and budgets, and require isolation. Without a policy file, legacy --auto
skips only the plan approval question. It must come from the user; an agent cannot
grant itself authority. Worktrees and script command sandboxing do not establish
a whole-session isolation boundary.

## Finish and publication

After an APPROVED review, substitute the task ID:

```text
/task-flow:task-finish <task-id>
```

Finish writes the summary, prepares the commit/PR text and commits locally after
the required gates. Enabled task records are generated deterministically. Push,
PR creation and worktree removal follow their separate authorization procedure;
plan approval or --auto grants no publication permission.

With project publication opt-in, a trusted operator outside the model session
receives the publication request and authorizes push and PR separately under the
effective policy. Credentials stay outside model sessions. Local completion, PR
publication and verified delivery into main are distinct; humans retain merge
decisions. See the [trusted publication handoff](../plugins/task-flow/skills/workflow/orchestration.md#trusted-publication-handoff-project-opt-in).

## Roadmap evidence

The README roadmap is maintained by hand. Task-level status takes precedence over
older epic summaries; epic membership supplies grouping and declared dependencies.
Arrows express roadmap sequence unless an epic declares a dependency. Finished means
the capability is implemented, not that every repository has enabled it.

| Group / status | Task evidence | Tracked implementation or current starting point |
| --- | --- | --- |
| Foundation — Finished | `ci-github-actions`, `task-record-in-git`, `task-epics`: done | `.github/workflows/ci.yml`, `plugins/task-flow/scripts/task_record.py`, `plugins/task-flow/scripts/epics.py` |
| Roles / CLI drivers — Finished | `cli-implementer-driver`, `external-role-delegation`, `implementer-model-routing`, `external-review-backlog`: done | `plugins/task-flow/scripts/models.py`, `plugins/task-flow/scripts/cli_driver.py`, `plugins/task-flow/scripts/role_run.py`; the benchmark harness was removed in favour of public model data |
| Control — Finished | `review-risk-flags`, `policy-config`, `enforce-gates-hooks`, `delivery-metrics`: done | `plugins/task-flow/scripts/risk.py`, `plugins/task-flow/scripts/policy.py`, `plugins/task-flow/scripts/phase_gate.py`, `plugins/task-flow/scripts/metrics.py` |
| Review / publication — Finished | `agent-pr-path`, `pr-review-ci`: done | `plugins/task-flow/scripts/publish_task.py`, `plugins/task-flow/scripts/pr_review.py`, `.github/workflows/pr-review.yml`; both capabilities are opt-in |
| Command sandboxing — Finished | `command-sandbox`: done | `plugins/task-flow/scripts/sandbox.py`, `plugins/task-flow/tests/test_sandbox.py` |
| Whole-session isolation — Waiting / unfinished | `unattended-isolation-2`: planned, awaiting approval; replaces `unattended-isolation`, stranded at checking round 1 | No delivered whole-session boundary; `plugins/task-flow/scripts/sandbox.py` covers script-launched commands only |
| Quality / design — Planned | `ci-scheduled-evals`, `designer-stage`, `designer-ui-mode`: new | Future work builds on `plugins/dev-baseline/evals/` and `.github/workflows/ci.yml`; these features are not delivered |
| Release CI — Planned | `release-pipeline-ci`: new | Local release exists in `plugins/task-flow/scripts/release.py`; a CI release pipeline remains planned |
| Maintenance / adoption — Planned | `maintain-signal-watch`, `task-intake`, `regression-from-escapes`, `repo-adoption-kit`: new | Existing `plugins/task-flow/scripts/metrics.py` supplies measurement, not the planned maintenance/intake loop |
| Deployment — Human decision | No implementation task scheduled | No deployment adapter delivered |

The isolation epic explicitly depends on completed command sandboxing before the
replacement isolation task. The superseded attempt is unfinished and is not counted
as actively progressing. No roadmap group is currently labelled In progress.
Quality/design and maintenance/adoption each retain their epic's internal task order.
Deployment remains a human decision. The opt-in SDLC generator is separate from this
manual roadmap and is not enabled by adding this guide.

Local preview check: Mermaid CLI 11.12.0 rendered the README graph to SVG and PNG
with all ten nodes and nine arrows. Visual inspection confirmed readable status
labels without clipping and a dotted deployment arrow; no rendering fixes were needed.
