# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Private, action-bound authorization evidence. Callers hold the task gate lock.

This is a hook/tool boundary for a private session, not an OS security boundary.
The entire repository-root storage directory, including temporary and consumed files,
is reserved for trusted hooks and explicit interactive terminal authorization.
"""

import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "approval-witness/1"
DIRECTORY = ".approval-witnesses"
VALID_SECONDS = 300


class Refusal(ValueError):
    """Missing or unusable evidence never supplies authority."""


def repository(project: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(project.resolve())).encode()).hexdigest()


def location(task: Path, action: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", task.name) or action not in (
        "approve",
        "grant",
    ):
        raise Refusal("invalid witness task or action")
    return task.parent / DIRECTORY / f"{task.name}-{action}.json"


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("duplicate witness key")
        result[key] = value
    return result


@contextmanager
def locked(task: Path) -> Iterator[None]:
    """Keep the shared lock inode in protected storage, including after crashes."""
    directory = location(task, "approve").parent
    directory.mkdir(exist_ok=True)
    with (directory / f".lock-{task.name}").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def validate(value: object, *, now: datetime) -> dict:
    keys = {
        "schema",
        "repository",
        "task",
        "plan",
        "attempt",
        "action",
        "ceiling",
        "channel",
        "session",
        "issued",
        "nonce",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise Refusal("malformed authorization witness")
    if (
        value["schema"] != SCHEMA
        or not all(isinstance(value[k], str) for k in keys - {"ceiling", "session"})
        or not all(
            re.fullmatch(r"[a-f0-9]{64}", value[k])
            for k in (
                "repository",
                "plan",
                "attempt",
                "nonce",
            )
        )
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value["task"])
        or value["action"] not in ("approve", "grant")
        or type(value["ceiling"]) is not int
        or (
            value["ceiling"] != 0
            if value["action"] == "approve"
            else value["ceiling"] < 1
        )
        or value["channel"] not in ("user-prompt", "terminal")
        or (
            not isinstance(value["session"], str) or not value["session"].strip()
            if value["channel"] == "user-prompt"
            else value["session"] is not None
        )
    ):
        raise Refusal("invalid authorization witness")
    try:
        stamp = datetime.fromisoformat(value["issued"])
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError("not UTC")
        age = (now - stamp).total_seconds()
    except (ValueError, TypeError) as exc:
        raise Refusal("invalid witness UTC time") from exc
    if not 0 <= age <= VALID_SECONDS:
        raise Refusal(
            "authorization witness expired or future-dated; repeat the user command"
        )
    return value


def issue(
    task: Path,
    action: str,
    plan: str,
    attempt: str,
    ceiling: int,
    *,
    channel: str,
    session: str | None,
    now: datetime | None = None,
) -> None:
    """Publish a fresh witness atomically while the caller holds the task lock."""
    instant = now if now is not None else datetime.now(UTC)
    value = validate(
        {
            "schema": SCHEMA,
            "repository": repository(task.parent.parent),
            "task": task.name,
            "plan": plan,
            "attempt": attempt,
            "action": action,
            "ceiling": ceiling,
            "channel": channel,
            "session": session,
            "issued": instant.isoformat(),
            "nonce": secrets.token_hex(32),
        },
        now=instant,
    )
    with locked(task):
        path = location(task, action)
        path.parent.mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".witness-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(value, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)


def consume(
    task: Path,
    action: str,
    plan: str,
    attempt: str,
    ceiling: int,
    *,
    now: datetime | None = None,
) -> dict:
    """Burn before audit/status publication. A failed operation needs fresh evidence."""
    path = location(task, action)
    try:
        with locked(task):
            value = validate(
                json.loads(path.read_bytes(), object_pairs_hook=unique_object),
                now=now if now is not None else datetime.now(UTC),
            )
            expected = {
                "repository": repository(task.parent.parent),
                "task": task.name,
                "action": action,
                "plan": plan,
                "attempt": attempt,
                "ceiling": ceiling,
            }
            if any(value[key] != wanted for key, wanted in expected.items()):
                raise Refusal(
                    "authorization witness does not match current task, plan, attempt, action or ceiling"
                )
            consumed = path.parent / ("consumed-" + value["nonce"] + ".json")
            if consumed.exists():
                raise Refusal("authorization witness already consumed")
            os.replace(path, consumed)
            return {
                "channel": value["channel"],
                "reference": hashlib.sha256(value["nonce"].encode()).hexdigest(),
            }
    except Refusal:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise Refusal(
            "authorization evidence unavailable; repeat the user command"
        ) from exc
