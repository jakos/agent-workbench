"""Non-blocking operation locks shared by all coordinating hosts."""

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path


class Busy(ValueError):
    """The operation could not obtain exclusive ownership."""


_HELD: ContextVar[frozenset[Path]] = ContextVar("workflow_locks", default=frozenset())


@contextmanager
def exclusive(work: Path, resource: str) -> Iterator[None]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", resource):
        raise Busy("invalid workflow lock identity")
    work = work.resolve()
    if not work.is_dir():
        raise Busy(f"workflow directory does not exist: {work}")
    path = work / f".operation-{resource}.lock"
    held = _HELD.get()
    if path in held:
        yield
        return
    try:
        handle = path.open("a+b")
    except OSError as exc:
        raise Busy(f"cannot open workflow lock for {resource}: {exc}") from exc
    with handle:
        try:
            if os.name == "nt":
                import msvcrt

                # Windows permits locking a byte beyond EOF. Keep the lock file
                # empty and never unlink it, including during error recovery.
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Busy(
                f"workflow lock unavailable: another Task Flow operation owns {resource}; retry when it exits"
            ) from exc
        token = _HELD.set(held | {path})
        try:
            yield
        finally:
            _HELD.reset(token)


def task_operation(task: Path):
    if any(path.is_symlink() or path.is_junction() for path in (task, task.parent)):
        raise Busy("task directory must not be a symbolic link")
    task = task.resolve()
    if task.parent.name != ".work" or not task.is_dir():
        raise Busy("task must be an existing ordinary .work directory")
    return exclusive(task.parent, f"task-{task.name}")


def task_locked(refusal):
    def decorate(function):
        @wraps(function)
        def run(task, *args, **kwargs):
            try:
                with task_operation(task):
                    return function(task, *args, **kwargs)
            except Busy as exc:
                raise refusal(str(exc)) from exc

        return run

    return decorate
