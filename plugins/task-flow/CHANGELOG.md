# Changelog

## 0.12.0 - 2026-11-02

### Features

- feat(task-flow): add bench.py publish for the results page, chart and history (8940cea6a727)

- feat(task-flow): add the first benchmark cases, check-case and a results log (ead1a2b9a854)

- feat(task-flow): add bench.py to compare implementer profiles on frozen cases (1bfb6e35580f)

- feat(task-flow): add task-doctor, a read-only environment and configuration check (01089de5aec9)

- feat(task-flow): post-edit feedback and prompt extras for the external implementer (b066e5e0cdfa)

- feat(task-flow): add an Anthropic Messages driver for the external implementer (9776b9896b44)

- feat(task-flow): add gates.py to run plan verification commands without a model (4ae798fa7aea)

- feat(task-flow): run Windows .cmd and .bat tools from the external implementer (be22872dbbc9)

- feat(task-flow): run existing tasks as a stack with task-autopilot --tasks (c06ee48dd802)

### Fixes

- fix(task-flow): accept scientific-notation costs in autopilot and tighten its docs and tests (d10cc1ffd455)

### Other

- docs: rewrite the README as a short task-flow front page (74238543f90d)

- docs: add Mermaid diagrams, a full setup guide and diagram checks (bb9117e1f763)

- docs: move the detailed task-flow documentation into docs/ subpages (59296249b790)

## 0.11.0 - 2026-11-01

### Features

- feat(task-flow): add /task-flow:task-release with a script-computed release (3e60b338fe64)

- feat(task-flow): account Claude tokens and external USD separately in the autopilot (6e92f7a97e40)

- feat(task-flow): choose each Claude role's model from models.toml (1987abbbdab9)

- feat(task-flow): implement.py reports per-turn timing, a heartbeat, retries and a progress trail (321402bd3d15)

- feat(task-flow): route the implementer role to implement.py when models.toml names a profile (8d33a2bbdf66)

- feat(task-flow): implement.py runs one implementer round on a non-Claude model (7ba81430147c)

### Fixes

- fix(task-flow): implement.py stages only what the round produced (d4471da80409)

### Other

- test(task-flow): make test_child_env_is_allowlisted deterministic (36d1cae69c99)

- docs: refresh task-flow, root and getting-started docs for the release (522d86d045fc)

- refactor(task-flow): one set of denial rules in rules.py for guard and implement.py (a69400fa1f36)
