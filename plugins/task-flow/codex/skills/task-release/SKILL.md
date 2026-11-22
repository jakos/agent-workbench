---
name: task-release
description: Verify and propose a release, then create the local release commit and annotated tag after approval.
---

Use the hook-authorized launcher from [Running Task Flow](../../run.md) for
all Task Flow script calls.
Follow the shared [Release procedure](../../../skills/workflow/orchestration.md#release--task-flowtask-release)
using its existing `release.py` and `policy.py` commands. Resolve the exact
target, use the same options at check and apply, wait for verification, and
show the dry-run proposal before any apply. A dry-run invocation ends there.
An actual release requires the explicit release-policy decision or the user's
approval of the shown proposal. If the proposal changes, show it again.
Any push needs a separate explicit request after the local release.
