# PR review evaluation

This is an offline evaluation of advisory PR review (`scripts/pr_review.py`).
`cases.json` freezes six defect cases and two clean controls. All eight are explicitly
**reconstructed** minimal counterexamples, not exact historical diffs. Each records
the full source fix commit, scenario, self-contained old/new input, SHA-256 input
digest, expected defect status and independent local oracle. No source commit or
fixture is executed. Provenance comes from these repository fixes:

| Source fix | Counterexample |
| --- | --- |
| `a659fc6759b7760dd0e1a7c68d29a48ebeafe14f` | Missing hook envelope; fixed-envelope control |
| `ef10a97e4b5a7d46f8b5d91d8824679c4337a17f` | Duplicate conventional hook registration |
| `0be1a2d959e46e5f3994354883886553142dcf71` | Project marketplace settings unexpectedly mutate global behavior |
| `c29f46d1db6607583306d28fa0ebeb05e6915eca` | Windows shell dependency, spaces in paths, BSD sed escaping; portable-command control |

The local oracles check the broken schema, global settings key, shell dependency,
unquoted path and platform-specific escape directly. They do not judge model prose.
Small controls isolate those properties; they are not claims that whole historical
revisions were defect-free. The global marketplace case also tests configuration
that loads but affects the wrong projects rather than enforcing the intended rules.

From the repository root, this command performs no network or generation requests:

```text
uv run --script plugins/task-flow/bench/pr_review_eval.py offline
```

The offline tests inject provider-neutral fake output, including failed attempts,
missed defects and clean controls. Fake scores prove plumbing only. Detection scoring
requires valid evidence at the case path and a scenario-specific term. Human semantic
inspection of each finding is still necessary; keyword matches are not proof.

## Separately authorized live run

No live run has been authorized. Read [results.md](results.md) before making a claim.
Prepare the exact manifest without contacting a model:

```text
uv run --script plugins/task-flow/bench/pr_review_eval.py prepare-live --max-usd 4
```

It prints eight input digests, policy/config digests, provider, pinned model, eight
maximum generation calls and the aggregate USD ceiling, plus a confirmation digest.
Review the concrete `cases.json`, production `REVIEW.md` and configuration with the
user. Changing the policy, inputs, model, configuration or ceiling changes the digest.
The checked-in configuration is disabled. An operator must separately prepare an
enabled configuration and name it with `--config` for both preparation and live use.
Only after explicit user confirmation, provision the named environment variable and
run the following **illustrative** command, substituting the confirmed digest and
that same configuration path:

```text
uv run --script plugins/task-flow/bench/pr_review_eval.py live --config enabled-review.toml --max-usd 4 --confirm CONFIRMED_MANIFEST_DIGEST --output live-review.json
```

There is at most one generation per case, no repair and no retry. The aggregate
preflight reserves worst-case input and output cost for all eight before any call;
failed attempts keep their reservation. The maximum accepted aggregate ceiling is
$4. This is separate from per-review `max_usd`. Unknown billed cost stays unknown,
including timeouts after sending a request. The report records mode, manifest,
input/policy digests, pinned provider/model, findings, detection evidence, false
positives, failed attempts, usage, known USD and the count with unknown cost. It
retains no raw responses, requests or credentials. A later authorized run should
summarize its report and human-checked findings in `results.md`.
