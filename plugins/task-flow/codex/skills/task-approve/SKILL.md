---
name: task-approve
description: Prepare or consume a signed Task Flow plan approval or round grant.
---

Follow [Operator approval](../../approval.md). The user supplies an exact task
ID, optionally a finite `--ceiling`, and both proof paths to consume approval.
With just the task ID and optional ceiling, prepare a request for the operator
to sign. Report the gate's result and leave any subsequent implementation to
the task runner.
