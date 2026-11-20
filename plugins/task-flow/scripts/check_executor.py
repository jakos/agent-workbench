"""Bound independent checks while retaining one result per input, in input order."""

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

Item = TypeVar("Item")
Result = TypeVar("Result")


def execute(
    items: Iterable[Item],
    run: Callable[[Item], Result],
    on_error: Callable[[Item, Exception], Result],
    workers: int = 1,
) -> list[Result]:
    """Run all items, converting ordinary worker exceptions with the runner's adapter.

    One worker runs directly on the calling thread. With more workers, only check
    execution moves to threads; callers retain preflight and ordered reporting.
    Interrupts and other BaseExceptions deliberately propagate.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")

    def guarded(item: Item) -> Result:
        try:
            return run(item)
        except Exception as exc:  # noqa: BLE001 - the runner reports every worker error
            return on_error(item, exc)

    if workers == 1:
        return [guarded(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(guarded, items))
