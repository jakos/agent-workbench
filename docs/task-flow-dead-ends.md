# task-flow dead ends and blocking paths

Audited commit: 085aa93

This page maps every path in task-flow that stopped work in recent tasks or that
can stop a fully automatic run, including a run in which task-flow changes itself.
It is the first step towards that mode: each dead end gets a remedy and a follow-up
task, and no enforcement is loosened by this page.

## Method

- Sources: the hooks (`plugins/task-flow/hooks/hooks.json:16`), the scripts they and
  the orchestration call (`phase_gate.py`, `rules.py`, `guard.py`, `policy.py`,
  `role_contracts.py`, `role_run.py`, `role_runtime.py`, `implement.py`, `models.py`,
  `prune_task.py`, `release.py`, `epics.py`, `gates.py`, `cli_driver.py`,
  `approval_witness.py`), the agent definitions, the
  [workflow contract](../plugins/task-flow/skills/workflow/SKILL.md) and
  `orchestration.md`, all read at the audited commit.
- Incidents 1 to 13 are the blocking incidents listed for this task (from the tasks
  gate-under-5-minutes, claude-subagent-effort, in-session-plan-approval,
  in-session-approval-2 and hook-status-read-false-positive, and the 0.17.0 and
  0.18.0 releases). Each was traced to the statement that enforces it. Entries marked
  `Incident: none` were found by reading the code and instructions; each names the
  statement that blocks, and the entry says when it was not observed in a run.
- `Automatic run` answers: can a run with no human at the keyboard get past this
  point within the existing rules? `yes` — always; `partly` — only on some routes or
  with a workaround the agent can apply itself; `no` — a human action is required
  (an edit, an approval, a deletion, or a process kill).
- Every `path:line` reference is checked by `plugins/task-flow/tests/test_docs.py`:
  the file must exist and the line must be within the file. A follow-up task that
  removes or moves a cited statement must update its entry here, or that test fails.
  The test checks structure and that each citation resolves, not that it points at
  the right statement; the line numbers are those of the audited commit.
- Truncation markers are written here with a letter (`[N more lines]`), never with
  digits, because a digit form makes this page itself an incomplete input for an
  external reviewer (DE04).

## Dead ends

### DE01: Witness guard refuses read-only shell commands
- Incident: 1
- Enforced at: `plugins/task-flow/scripts/rules.py:276`, `plugins/task-flow/scripts/rules.py:296`, `plugins/task-flow/scripts/rules.py:214`, `plugins/task-flow/scripts/guard.py:67`, `plugins/task-flow/scripts/phase_gate.py:1199`
- How it blocks: `witness_command` treats a command as mutating when it contains any `>` (also `2>&1`) or a word such as `sed`, `replace`, `del`, `ren` or `cp`; then every word containing `*` or `?` whose prefix has no `/` resolves to `.`, and `witness_write` protects `.`, `..` and `.work` as ancestors of `.work/.approval-witnesses/`, so a read-only `grep` with a `.*` pattern and a redirect, or a `sed -n` over `*.py`, is refused with "model tools cannot replace or delete authorization witness ancestors"; the same applies to a slash-less glob deletion such as `rm *.tmp` run with the task directory as cwd (it resolves to `.`) and to `rm -rf .work/*` (it resolves to `.work`), which is how scratch files under `.work/` came to be left for the human to delete. A deletion through an explicit path, such as `rm -rf .work/<id>/<dir>/*` from the repository root, resolves to `.work/<id>/<dir>` and is not refused by `witness_command`.
- Automatic run: partly — the Read, Grep, Glob, Write and Edit tools, plain commands without redirects or globs, and glob deletions written with an explicit directory path get through, but an agent that uses ordinary shell idioms (redirects, slash-less globs) loses turns until it rewrites the command.
- Remedy: parse the command into segments and count `>` as a mutation only for its target operand; resolve wildcards only in operands of a mutating program; keep refusing any operand that is, contains or is an ancestor of `.work/.approval-witnesses/`; route scratch cleanup through `prune_task.py` instead of shell deletion.
- Follow-up: hook-command-precision

### DE02: Chained epic removal refused as a dangling dependency
- Incident: 2
- Enforced at: `plugins/task-flow/scripts/epics.py:931`, `plugins/task-flow/scripts/epics.py:932`
- How it blocks: `cmd_remove` checks every remaining row for a parent in the removal set, but it also checks the rows being removed, so removing a parent together with its only child (a chain) is refused as "dangling dependency" although no remaining row would depend on a removed one; in the incident `epic.md` was edited by hand.
- Automatic run: partly — removing the rows one at a time, leaf first, passes the check; removing them in one call is refused.
- Remedy: skip rows whose task id is itself in the removal set; add a test for removing a chain and keep the refusal when a remaining row depends on a removed one.
- Follow-up: epic-chained-remove

### DE03: Plans that the implementer cannot satisfy, or with worktree-relative paths
- Incident: 3
- Enforced at: `plugins/task-flow/agents/planner.md:22`, `plugins/task-flow/scripts/phase_gate.py:844`, `plugins/task-flow/scripts/phase_gate.py:621`, `plugins/task-flow/skills/workflow/SKILL.md:351`
- How it blocks: the planner is told only that criteria must be decidable; nothing checks that the implementer can produce the evidence or that cited paths are repository-relative, so a wrong plan reaches implementation; the implementer may only report `BLOCKED:`, which returns to the human; a plan edit changes the plan digest, so the approval found by `authorize` no longer matches and every protected action refuses until a new approval; a blocked or invalid plan makes every gate action refuse.
- Automatic run: no — getting out needs a human edit of `task.md`, a re-plan and, under a policy that asks for human approval, a fresh `/task-flow:task-approve`.
- Remedy: validate plan inputs mechanically before approval (no absolute or worktree-relative paths, no `manual:` verification when the task forbids manual criteria, every cited test file present or created by a step); after `BLOCKED:` let the orchestrator append the reason to `task.md` as an audited revision and re-plan, with approval still decided by policy.
- Follow-up: plan-contract-repair

### DE04: Truncation-marker text in a legitimate file refuses external review
- Incident: 4
- Enforced at: `plugins/task-flow/scripts/role_run.py:206`, `plugins/task-flow/scripts/role_run.py:381`, `plugins/task-flow/scripts/role_contracts.py:84`, `plugins/task-flow/scripts/implement.py:1041`, `plugins/task-flow/tests/test_implement.py:888`
- How it blocks: `read_required` and the touched-file loop refuse any input that contains text matching `TRUNCATION_MARKERS`; the implementer's own post-edit output produces the `...[N more lines]` form, and a test asserts it literally, so any change that touches that test file (or a document quoting the marker) is refused by the external reviewer before model contact, on every resume.
- Automatic run: no — the same inputs refuse every time; only a Claude reviewer, or changing the file so the literal disappears, gets past.
- Remedy: decide completeness by provenance, not by text: check markers only in content the host itself truncated (tool output, model artifacts) and read repository files whole by size limit alone; keep refusing oversized and binary inputs.
- Follow-up: review-input-contract

### DE05: Review Scope must be exactly none for approval
- Incident: 5
- Enforced at: `plugins/task-flow/scripts/role_contracts.py:771`, `plugins/task-flow/scripts/role_contracts.py:797`, `plugins/task-flow/scripts/phase_gate.py:533`
- How it blocks: `validate_review` counts any Scope other than exactly `none` as a must-fix item, so an approving review that writes `none — <explanation>` contradicts itself and is refused; the gate re-validates the review on the transition to review-approved, so a reviewer edit that changed Scope forced a revert and a full re-check.
- Automatic run: partly — the external runner repairs once and the orchestrator can rerun a Claude reviewer, but a reviewer that keeps explaining an empty Scope is refused every time, and the incident needed a manual revert and a full re-check.
- Remedy: accept `none` followed by a dash and an explanation as "no out-of-scope change"; keep every other Scope text blocking an approval.
- Follow-up: review-input-contract

### DE06: Multi-line plan steps rejected with one reason per line
- Incident: 6
- Enforced at: `plugins/task-flow/scripts/role_contracts.py:263`, `plugins/task-flow/scripts/role_contracts.py:310`, `plugins/task-flow/skills/workflow/orchestration.md:303`, `plugins/task-flow/skills/workflow/orchestration.md:296`
- How it blocks: `_check_steps` refuses every continuation line as "not a numbered step line" (criteria accept indented continuations, steps do not), so a readable multi-line plan collects one reason per line; the external planner gets one automatic repair, but the Claude planner subagent writes `plan.md` directly with no validation loop, so repair needed a manual message.
- Automatic run: partly — an external planner run repairs once by itself; the Claude planner route has no automatic repair.
- Remedy: accept indented continuation lines under Steps exactly as under criteria; after a Claude planner run, validate with `role_contracts.py` and give the planner one automatic repair with the reasons, as `role_run.py` does.
- Follow-up: plan-contract-repair

### DE07: Implementer cannot write its Log line
- Incident: 7
- Enforced at: `plugins/task-flow/agents/implementer.md:30`, `plugins/task-flow/scripts/phase_gate.py:1209`, `plugins/task-flow/scripts/phase_gate.py:1347`, `plugins/task-flow/scripts/phase_gate.py:1004`, `plugins/task-flow/scripts/phase_gate.py:979`
- How it blocks: the implementer is told to append to `status.md`, but the hook refuses every Edit or Write of a task's `status.md` as a direct status mutation; the orchestrator records the line with `phase_gate.py note`, which calls `authorize` and so works only after approval; the `blocked` transition needs a `BLOCKED:` line in the Log, which only that relay can write.
- Automatic run: partly — the orchestrator relays the line through `note` after approval; a subagent that follows its instructions literally is refused, and a relay before approval is impossible.
- Remedy: change the Claude implementer contract to return its Log or `BLOCKED:` line in the final report (as `implement.py` already writes them for external rounds) and let the orchestrator record it through `note`; keep direct writes of `status.md` refused.
- Follow-up: implementer-log-channel

### DE08: Implementer routing pin refuses after a configuration change
- Incident: 8
- Enforced at: `plugins/task-flow/scripts/models.py:974`, `plugins/task-flow/scripts/models.py:998`
- How it blocks: once an attempt is pinned, any change to the routing inputs (configured implementer, thresholds, plan digest) makes `attempt_record` raise, so `models.py roles --task-dir` refuses and the implement phase cannot start; enabling routing on an attempt that has started is refused too; only an authorized restart or epic retry clears it.
- Automatic run: no — the pin is cleared only by restoring the old inputs or by a retry the user authorizes.
- Remedy: let the preview report drift without failing, and add a gate-recorded re-pin at a round boundary under policy that never changes the model inside a round; keep refusing silent drift.
- Follow-up: routing-pin-repin

### DE09: Approval needs a typed human command within five minutes
- Incident: 9
- Enforced at: `plugins/task-flow/scripts/approval_witness.py:25`, `plugins/task-flow/scripts/approval_witness.py:131`, `plugins/task-flow/scripts/phase_gate.py:1256`, `plugins/task-flow/scripts/phase_gate.py:878`, `plugins/task-flow/skills/task-approve/SKILL.md:5`, `plugins/task-flow/skills/task-release/SKILL.md:5`
- How it blocks: human approval is evidence issued only by the real prompt hook for the exact text `/task-flow:task-approve <id>`, valid for 300 seconds and single-use; the approve and release skills cannot be invoked by the model; when policy requires human approval, a human away from the machine stops the run.
- Automatic run: partly — a policy with `plan_approval = auto` or `auto-low-risk` approves without a human; release and extra-round grants always need a human.
- Remedy: offer a documented self-hosting policy (auto approval for low-risk plans, human for the rest) and let autopilot continue with independent tasks while one waits; keep the witness lifetime, the exact-command rule and human-only release.
- Follow-up: unattended-authorization

### DE10: Leaked test processes lock the worktree
- Incident: 10
- Enforced at: `plugins/task-flow/tests/test_sandbox.py:650`, `plugins/task-flow/tests/test_sandbox.py:1017`, `plugins/task-flow/scripts/prune_task.py:78`, `plugins/task-flow/skills/workflow/orchestration.md:944`
- How it blocks: the sandbox timeout tests start a grandchild that loops forever (writing a heartbeat file) or sleeps for ten minutes; when its parent has already exited, nothing reaps it, so `python.exe` keeps files in the worktree open for days; `git worktree remove` fails and `prune_task.py` leaves `worktree/` to the human question.
- Automatic run: no — a human has to find and end the processes, then remove the worktree.
- Remedy: bound every test child (exit when a sentinel file disappears or after a short deadline) and start it in a job object or process group the test kills in cleanup; let finish report the processes holding the worktree instead of failing silently.
- Follow-up: process-tree-cleanup

### DE11: Release refuses a tree dirtied outside a task
- Incident: 11
- Enforced at: `plugins/task-flow/scripts/release.py:613`, `plugins/task-flow/scripts/release.py:618`
- How it blocks: `release.py check` refuses on any `git status --porcelain` output in the root checkout, so a configuration change made outside a task (for example a local `.claude/task-flow.models.toml` edit) blocks the release until it is committed or stashed.
- Automatic run: no — release is human-gated anyway, and the dirty file needs a human decision.
- Remedy: name the dirty paths in the refusal and whether they are inside the target's scope; route configuration changes through a task; keep refusing a dirty tree.
- Follow-up: release-dirty-scope

### DE12: The installed plugin is older than the repository
- Incident: 12
- Enforced at: `plugins/task-flow/hooks/hooks.json:9`, `plugins/task-flow/skills/workflow/orchestration.md:357`, `plugins/task-flow/scripts/models.py:488`, `plugins/task-flow/scripts/models.py:517`, `plugins/task-flow/scripts/doctor.py:721`
- How it blocks: hooks, gates and every orchestration script run from `${CLAUDE_PLUGIN_ROOT}`, the installed copy, which rejects unknown configuration keys and tables; a task that adds a key (`[gates] workers`, `[roles.effort]`) and uses it in the repository configuration is refused by the old copy until a release and a plugin update; the doctor only warns.
- Automatic run: no — the release is human-only and the plugin update is a human action.
- Remedy: record the minimum plugin version a configuration file needs and refuse with an "update the plugin" message instead of an unknown-key error; never adopt new configuration keys in the same task that introduces them; make release-and-update an explicit epic step.
- Follow-up: self-hosting-copy-selection

### DE13: Process-tree kill denied in the Codex sandbox on Windows
- Incident: 13
- Enforced at: `plugins/task-flow/scripts/gates.py:307`, `plugins/task-flow/scripts/cli_driver.py:380`, `plugins/task-flow/tests/test_gates.py:497`, `plugins/task-flow/tests/test_cli_driver.py:376`
- How it blocks: on Windows a timed-out command is killed with `taskkill /F /T`; that sandbox denies it, only the direct child is killed, and the timeout tests that check the whole tree is gone fail there on every run.
- Automatic run: no — the same tests fail deterministically inside that sandbox.
- Remedy: kill trees through a job object the runner owns, with `taskkill` as fallback, and probe the capability so the tests report a clear skip reason where neither is available.
- Follow-up: process-tree-cleanup

### DE14: Read-only shell commands that mention status.md are refused
- Incident: none
- Enforced at: `plugins/task-flow/scripts/phase_gate.py:1213`, `plugins/task-flow/scripts/phase_gate.py:1336`, `plugins/task-flow/scripts/phase_gate.py:1347`
- How it blocks: a shell command counts as a status mutation when it names `status.md` and contains `>` or a word like `python` or `sed`, so `cat <task>/status.md 2>&1` is refused, either as "missing or ambiguous explicit task context" (several tasks named) or as a direct status mutation; the task hook-status-read-false-positive records this and is not yet implemented.
- Automatic run: partly — the Read tool reads `status.md` without the hook; shell reads with a redirect are refused.
- Remedy: count a redirect only when its target is a `status.md` path and treat pure readers as reads; keep every write form refused.
- Follow-up: hook-command-precision

### DE15: Autonomous preset refuses every gated action
- Incident: none
- Enforced at: `plugins/task-flow/scripts/policy.py:217`, `plugins/task-flow/scripts/phase_gate.py:805`, `plugins/task-flow/scripts/phase_gate.py:1285`
- How it blocks: `policy.effective` refuses unless it is told that isolation is active when the preset is `autonomous` or `isolation_required` is set, but `phase_gate.py` always calls it without an isolation argument, so every gate decision and every hooked launch is refused with "missing isolation backend" even when a sandbox backend is configured; found by reading, not observed (no test covers the combination).
- Automatic run: no — the preset meant for unattended runs cannot pass the gate at all.
- Remedy: resolve the active isolation backend in `phase_gate.py` from the validated sandbox policy and pass it to `effective`; keep the refusal when no backend is active.
- Follow-up: gate-isolation-backend

### DE16: Interrupted gate log needs human recovery in a separate terminal
- Incident: none
- Enforced at: `plugins/task-flow/scripts/phase_gate.py:306`, `plugins/task-flow/scripts/phase_gate.py:483`, `plugins/task-flow/scripts/phase_gate.py:1295`, `plugins/task-flow/scripts/phase_gate.py:1091`
- How it blocks: a `gates.jsonl` whose last line was cut (crash, full disk, killed process) refuses every protected action; recovery needs `recover --source user`, which the hook refuses from model tools, and any read or write error of the evidence refuses the protected operation as well.
- Automatic run: no — only a human in a separate terminal can recover; found by reading, not observed.
- Remedy: let trusted code tombstone a final fragment that is not valid JSON (it can carry no authority) with policy source, and keep human recovery for anything else.
- Follow-up: unattended-authorization

### DE17: Round cap is extended only by a human grant
- Incident: none
- Enforced at: `plugins/task-flow/scripts/phase_gate.py:862`, `plugins/task-flow/scripts/phase_gate.py:923`
- How it blocks: after the last allowed round without APPROVED the run escalates, and only `/task-flow:task-grant` typed by a human raises the ceiling.
- Automatic run: no — this is an intended stop of every run.
- Remedy: keep it; an automatic driver should escalate the task and continue with independent work.
- Follow-up: none — the round cap and its human-only extension are intended stops (SKILL.md, Stops), and removing them would loosen enforcement.

### DE18: Epic retry needs explicit user authorization
- Incident: none
- Enforced at: `plugins/task-flow/skills/workflow/SKILL.md:117`, `plugins/task-flow/skills/workflow/orchestration.md:865`, `plugins/task-flow/scripts/models.py:951`
- How it blocks: a failed epic member is reset to round 0 only with `--authorized`, the user's explicit authorization, and `--auto` never grants it; the routing pin treats only such a reset as a new attempt.
- Automatic run: no — a failed member of an epic waits for the human.
- Remedy: an explicit, default-off policy key allowing at most one automatic retry per task, recorded in History like the human authorization; never more than the key allows.
- Follow-up: unattended-authorization

### DE19: Hooks block every tool call when uv is slow
- Incident: none
- Enforced at: `plugins/task-flow/hooks/hooks.json:24`, `plugins/task-flow/hooks/hooks.json:25`, `plugins/task-flow/hooks/hooks.json:36`
- How it blocks: two `uv run --script` hooks run before every Edit, Write and shell call with a 15 second timeout and `onFailure: block`; with a cold uv cache (Python 3.14 to download) or a slow machine they time out and the tool call is blocked, including the shell call that would warm the cache; found by reading, not observed.
- Automatic run: partly — a warm cache passes; a cold one blocks until a human warms it.
- Remedy: have `task-doctor` warm the cache and report hook latency, and merge the two PreToolUse hooks into one process; keep `onFailure: block`.
- Follow-up: hook-command-precision

## Self-hosting

When task-flow changes task-flow, three copies exist at once: the installed plugin
(`${CLAUDE_PLUGIN_ROOT}`, the copy Claude Code loaded), the root checkout (the
repository on the base branch, holding `.claude/` configuration and `.work/`), and
the task worktree (the branch being changed).

| Phase | Governing copy | Evidence |
| --- | --- | --- |
| hooks | installed plugin | `plugins/task-flow/hooks/hooks.json:23`, `plugins/task-flow/hooks/hooks.json:35` |
| plan | installed plugin (agent and runner), root checkout (`.claude/task-flow.md`) | `plugins/task-flow/skills/workflow/orchestration.md:296`, `plugins/task-flow/agents/planner.md:14` |
| approve | installed plugin (hook and gate), root checkout (policy file) | `plugins/task-flow/scripts/phase_gate.py:1256`, `plugins/task-flow/scripts/phase_gate.py:805` |
| baseline | installed plugin (runner), task worktree (tests at the base) | `plugins/task-flow/skills/workflow/orchestration.md:348`, `plugins/task-flow/skills/workflow/orchestration.md:357` |
| implement | installed plugin (agent, guard), root checkout (routing configuration), task worktree (edits) | `plugins/task-flow/skills/workflow/orchestration.md:375`, `plugins/task-flow/skills/workflow/orchestration.md:391`, `plugins/task-flow/tests/test_implement.py:398` |
| check | installed plugin (`gates.py`), root checkout (`[gates]` configuration), task worktree (the new tests) | `plugins/task-flow/skills/workflow/orchestration.md:427`, `plugins/task-flow/tests/test_models.py:91` |
| review | installed plugin (agent, runner, contract), root checkout (risk configuration at the base), task worktree (diff) | `plugins/task-flow/skills/workflow/orchestration.md:443`, `plugins/task-flow/skills/workflow/orchestration.md:453`, `plugins/task-flow/scripts/role_run.py:345` |
| finish | installed plugin (finish gate, prune), task worktree (commit) | `plugins/task-flow/scripts/phase_gate.py:1357`, `plugins/task-flow/skills/workflow/orchestration.md:938`, `plugins/task-flow/skills/workflow/orchestration.md:947` |
| release | installed plugin (`release.py`), root checkout (release configuration, clean tree) | `plugins/task-flow/skills/workflow/orchestration.md:1010`, `plugins/task-flow/scripts/release.py:613` |

The bootstrap problem: every enforcing piece — hooks, `phase_gate.py`, `rules.py`,
`role_contracts.py`, the agent definitions and `gates.py` — runs from the installed
plugin, while the change lives in the task worktree. A task that changes a hook, a
gate, a contract or an agent is therefore planned, checked, reviewed and finished by
the old copy. Its new tests run against the new scripts in the worktree, but the
process around them does not use the new code until the change is merged, released
with `/task-flow:task-release` (human only) and the plugin is updated. Two
consequences follow. First, a fix to a dead end cannot help the task that makes it:
a task that removes DE01 is still refused by DE01 while it runs. Second, a task that
starts using a new configuration key in the root checkout breaks the old copy for
every other task (DE12). The follow-up epic is therefore ordered so that each fix is
released before a later task depends on it, and no task adopts a new configuration
key in the same change that introduces it.

## Follow-up epic

Ordered so that the dead ends that stop an automatic run of task-flow on itself are
removed first: the hook false positives and the Log channel affect every task, the
isolation fix is a precondition of the autonomous preset, and the plan and review
contracts decide whether a round can complete without a human message.

| # | Task | Depends on |
| --- | --- | --- |
| 1 | hook-command-precision | - |
| 2 | implementer-log-channel | hook-command-precision |
| 3 | gate-isolation-backend | - |
| 4 | plan-contract-repair | - |
| 5 | review-input-contract | plan-contract-repair |
| 6 | self-hosting-copy-selection | - |
| 7 | unattended-authorization | gate-isolation-backend |
| 8 | routing-pin-repin | - |
| 9 | process-tree-cleanup | - |
| 10 | release-dirty-scope | - |
| 11 | epic-chained-remove | - |

### hook-command-precision

Make the witness and status-mutation hooks judge operands instead of raw command text
(DE01, DE14) and make hook start-up reliable (DE19). Must not loosen: every write,
deletion or replacement of `.work/.approval-witnesses/` or its ancestors, and every
write form of a task's `status.md`, stays refused; `onFailure: block` stays.

### implementer-log-channel

Return the Claude implementer's Log and `BLOCKED:` lines in its report and record them
through `phase_gate.py note` (DE07). Must not loosen: agents still cannot write
`status.md`, and `note` still requires the current plan approval.

### gate-isolation-backend

Pass the active isolation backend from the validated sandbox policy to
`policy.effective` in `phase_gate.py` (DE15). Must not loosen: without an active
backend the autonomous preset and `isolation_required` still refuse.

### plan-contract-repair

Accept indented step continuations, validate Claude planner output with one automatic
repair, and lint plans for inputs the implementer cannot produce (DE03, DE06). Must
not loosen: approval stays bound to the plan digest, and a changed plan still needs a
new approval decided by policy.

### review-input-contract

Decide input completeness by provenance rather than marker text and accept an
explained `none` Scope (DE04, DE05). Must not loosen: oversized, binary and
host-truncated inputs stay refused, and any real out-of-scope change still blocks
approval.

### self-hosting-copy-selection

Add a minimum-plugin-version handshake to configuration files and document the
release-then-adopt order for changes to enforcing code (DE12). Must not loosen:
unknown keys remain errors, and the installed copy keeps enforcing hooks and gates.

### unattended-authorization

Provide a documented self-hosting policy for unattended runs, automatic tombstoning of
a non-JSON gate-log fragment, and an opt-in, bounded automatic epic retry (DE09, DE16,
DE18). Must not loosen: the witness lifetime and exact-command rule, human-only
release and grants, and human recovery of any parsable log line stay as they are.

### routing-pin-repin

Report routing drift in the preview and allow a gate-recorded re-pin at a round
boundary (DE08). Must not loosen: the model never changes inside a round, and silent
drift still refuses.

### process-tree-cleanup

Kill process trees through a job object with `taskkill` as fallback, and bound and reap
every test child process (DE10, DE13). Must not loosen: timed-out commands still fail
and their whole tree is still ended.

### release-dirty-scope

Name the dirty paths and their scope in the release refusal (DE11). Must not loosen:
a dirty tree still refuses a release.

### epic-chained-remove

Allow removing a chain of rows together when no remaining row depends on a removed
one (DE02). Must not loosen: started tasks still cannot be removed, and a remaining
row with a removed parent still refuses.
