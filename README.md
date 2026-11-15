# agent-workbench

Claude Code plugins for everyday development and reviewed task workflows.

## Plugins

- **dev-baseline** bootstraps repositories, sets up Python tooling and automatic
  formatting, guides systematic debugging, and provides an independent code reviewer.
- **task-flow** runs planned changes in Git worktrees. Its AI-native workflow uses
  fresh-context roles and file-based handoffs, plan approval, deterministic baseline
  and round checks, independent criterion review, and policy-controlled autonomy.

See [the usage guide](docs/how-to.md) for setup, the task loop and roadmap evidence.

## Install

Requires Claude Code, Git, and [uv](https://docs.astral.sh/uv/).

```text
/plugin marketplace add jakos/agent-workbench
/plugin install dev-baseline@agent-workbench
/plugin install task-flow@agent-workbench
```

## Roadmap

This is a manually maintained capability snapshot. Arrows show roadmap sequence;
the guide records task evidence and explains declared epic dependencies.

```mermaid
flowchart TD
    foundation["Finished: foundation, CI, task records, epics"]
    roles["Finished: configurable roles and CLI benchmarking"]
    control["Finished: risk, policy, enforced gates, metrics"]
    review["Finished: opt-in publication and advisory PR review"]
    commands["Finished: command sandboxing"]
    isolation["Waiting / unfinished: whole-session isolation; replacement awaits approval"]
    quality["Planned: quality benchmarks, test evaluation, scheduled evals, design / UI roles"]
    release["Planned: release CI"]
    maintain["Planned: maintenance signals, intake, regression feedback, adoption"]
    deploy["Human decision: deployment; no scheduled implementation"]
    foundation --> roles --> control --> review --> commands
    commands --> isolation --> quality --> release --> maintain
    maintain -.-> deploy
```

Legend: **Finished** = implemented; **In progress** = active work (none shown);
**Waiting / unfinished** = pending approval or completion; **Planned** = future work;
**Human decision** = no implementation scheduled. Status labels do not depend on colour.
The superseded isolation attempt is unfinished, not actively advancing.

## License

[MIT](LICENSE)
