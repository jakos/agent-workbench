# Changelog

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
