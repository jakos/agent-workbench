"""Independent copies of an initialized, immutable standalone Git fixture."""

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path


class RepositoryTemplate:
    """Build once per test class; copy all Git metadata before adding any worktree.

    Never share indexes, object files via hardlinks, config or worktree registration.
    The builder must create a standalone repository without absolute path references.
    """

    def __init__(self, build: Callable[[Path], None]) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="repository template ")
        self._source = Path(self._temporary.name) / "repo"
        self._source.mkdir()
        try:
            build(self._source)
        except BaseException:
            self.close()
            raise

    def copy_to(self, destination: Path) -> None:
        """Copy into a new directory; subsequent mutations belong to that test only."""
        shutil.copytree(self._source, destination)

    def close(self) -> None:
        self._temporary.cleanup()
