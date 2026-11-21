"""Verify plan-bound operator approval with OpenSSH signatures."""

import hashlib
import json
import os
import re
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import approval_witness

NAMESPACE = "task-flow-approval"
PRINCIPAL = "task-flow-operator"


def scope(task: Path, action: str, plan: str, attempt: str, ceiling: int) -> dict:
    return {
        "schema": "task-flow-signed-approval/1",
        "repository": approval_witness.repository(task.parent.parent),
        "task": task.name,
        "action": action,
        "plan": plan,
        "attempt": attempt,
        "ceiling": ceiling,
    }


def request(binding: dict) -> dict:
    return {
        **binding,
        "issued": datetime.now(UTC).isoformat(),
        "nonce": secrets.token_hex(32),
    }


def operator_signers(project: Path) -> Path:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "config",
            "--local",
            "--no-includes",
            "--get-all",
            "task-flow.allowedSignersFile",
        ],
        env={
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
        },
        capture_output=True,
        check=False,
        timeout=15,
    )
    paths = result.stdout.decode("utf-8").splitlines()
    if result.returncode or len(paths) != 1:
        raise ValueError("configure task-flow.allowedSignersFile in local Git config")
    signers = Path(paths[0])
    if (
        not signers.is_absolute()
        or not signers.is_file()
        or signers.resolve().is_relative_to(project)
    ):
        raise ValueError(
            "operator signers must be an absolute file outside the repository"
        )
    return signers


def consume(binding: dict, task: Path, payload: Path, signature: Path) -> dict:
    try:
        with payload.open("rb") as handle:
            data = handle.read(4097)
        if len(data) > 4096:
            raise ValueError("oversized approval request")
        value = json.loads(data, object_pairs_hook=approval_witness.unique_object)
        if not isinstance(value, dict) or set(value) != {*binding, "issued", "nonce"}:
            raise ValueError("invalid approval request")
        if any(
            type(value[k]) is not type(v) or value[k] != v for k, v in binding.items()
        ):
            raise ValueError(
                "signed request does not match the current task, plan, attempt or action"
            )
        nonce = value["nonce"]
        if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{64}", nonce):
            raise ValueError("invalid approval nonce")
        stamp = datetime.fromisoformat(value["issued"])
        if stamp.utcoffset() != UTC.utcoffset(stamp):
            raise ValueError("invalid approval timestamp")
        if (
            not 0
            <= (datetime.now(UTC) - stamp).total_seconds()
            <= approval_witness.VALID_SECONDS
        ):
            raise ValueError("signed request expired or future-dated")
        signers = operator_signers(task.parent.parent)
        verification = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(signers),
                "-I",
                PRINCIPAL,
                "-n",
                NAMESPACE,
                "-s",
                str(signature.resolve()),
            ],
            input=data,
            capture_output=True,
            check=False,
            timeout=15,
        )
        if verification.returncode:
            raise ValueError("operator signature verification failed")
        with approval_witness.locked(task):
            directory = approval_witness.location(task, binding["action"]).parent
            with (directory / f"consumed-{nonce}.json").open("xb") as handle:
                handle.write(b'{"channel":"ssh-signature"}\n')
    except FileExistsError as exc:
        raise approval_witness.Refusal("signed request already consumed") from exc
    except (ValueError, TypeError, OSError, subprocess.SubprocessError) as exc:
        raise approval_witness.Refusal(f"signed approval refused: {exc}") from exc
    return {
        "channel": "ssh-signature",
        "reference": hashlib.sha256(nonce.encode()).hexdigest(),
    }
