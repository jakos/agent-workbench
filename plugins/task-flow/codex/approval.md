# Operator approval

Codex uses a detached OpenSSH signature as proof of operator approval. The operator
keeps the signing key and any signing agent outside the model environment. Create
an allowed-signers file outside the checkout with one line containing
`task-flow-operator` followed by the operator's public SSH key in OpenSSH format.
In a separate terminal at the repository root, configure it once:

```text
git config --local task-flow.allowedSignersFile "<absolute-allowed-signers-path>"
```

Keep the model's Git metadata and allowed-signers file read-only, and use a
trusted Git and OpenSSH installation. A dedicated passphrase-protected or
hardware-backed key is suitable. Claude Code's existing prompt approval also works.

Use the current Task Flow root as described in [run.md](run.md). After showing the
exact plan, prepare a fresh request with a unique output filename under the task
directory. Use absolute paths and run from the project root:

```text
uv run --quiet --script "<task-flow-root>/scripts/codex_host.py" phase_gate.py approval-request --task-dir "<task-dir>" --output "<request.json>"
```

For a grant, include `--ceiling <number>` with a finite total round ceiling.
Show the request path, plan path, and signing command. The operator reviews the
plan, verifies the request identifies that task and plan, and signs it in a
separate terminal within five minutes:

```text
ssh-keygen -Y sign -f "<operator-key>" -n task-flow-approval "<request.json>"
```

The operator then invokes the `task-approve` skill with the task ID, request path
and signature path (normally `<request.json>.sig`). For a grant they also supply
the same `--ceiling`. Run:

```text
uv run --quiet --script "<task-flow-root>/scripts/codex_host.py" phase_gate.py approve --source user --task-dir "<task-dir>" --request-file "<request.json>" --signature "<request.json>.sig"
```

For a grant use `grant` and add the same `--ceiling <number>`.
The gate verifies the signature, task, current plan, attempt, action, ceiling,
freshness, and single use. On refusal, show the reason. After an expired or changed
request, generate a new file for the operator to review and sign. A Codex chat
reply supplies no plan approval. Never sign or configure a signing key on the
operator's behalf. Use a current checkout or release on both hosts when sharing
tasks with signed approval records.
