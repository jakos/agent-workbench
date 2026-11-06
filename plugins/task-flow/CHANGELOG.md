# Changelog

## 0.13.0 - 2026-11-06

### Features

- feat(bench): publish run time next to cost and quality (edb589043f8e)

- feat(bench): run agent command-line tools in the benchmark (8a9ab5a1b32f)

- feat(bench): add a repeatable benchmark protocol with per-round hidden probes (75faea38eca0)

- feat(task-flow): run the backlog and reviewer roles through an external profile (f344a8959d4c)

- feat(task-flow): choose the implementer per task by plan size (4426ff497e8a)

- feat(task-flow): run the planner role through an external profile (0e274f40556b)

- feat(task-flow): add a generic cli driver to implement.py (eb50a6dedbf6)

- feat(task-flow): add task epics to group, run and inspect related tasks (3762010cc962)

- feat(task-flow): add an opt-in finish record and commit trailers (db4c4b2b5616)

- feat(task-flow): publish notes for merged runs and mixed turn limits (375b2f822572)

- feat(task-flow): add bench.py merge and fill the All table in report.md (596565f690db)

### Fixes

- fix(task-flow): make two tests independent of the host (8518e8381b15)

- fix(task-flow): make the doctor and cli driver tests independent of the host (5070d8e1fa63)

- fix(task-flow): make the test suite portable across Windows, Linux and drives (2b01f58b6a0d)

### Other

- docs: state the AI-native SDLC purpose and generate the stage status view (b65d89da0836)

- ci: run the task-flow gates on GitHub Actions (a200303d5265)

- docs(task-flow): save the first benchmark runs (383d3414bf2e)

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
