"""A small cross-platform exclusive file lock.

Two collector processes must never run a throughput test at the same time (each
would measure half the line), and two publishes must never share the scratch
repo. Until on-demand tests existed the timers kept them apart by schedule
alone; now `control` can start either at any minute, so they take a lock.

The lock is advisory and held on an open file handle, so the OS drops it when
the process exits, whatever way it exits — a crash can never leave it stuck.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger("broadband.lock")


class Busy(RuntimeError):
    """The lock is still held by another process after waiting."""


if os.name == "nt":
    import msvcrt

    def _try_lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def held(path: Path, wait: float = 0.0, poll: float = 1.0) -> Iterator[None]:
    """Hold an exclusive lock on path, waiting up to `wait` seconds for it.

    Raises Busy if another process still holds it after that. A lock file this
    account cannot open (made by a manual run as someone else) is logged and
    ignored rather than blocking collection.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")  # noqa: SIM115 - closed in finally
    except OSError as exc:
        log.warning("cannot open lock %s (%s) — continuing without it", path, exc)
        yield
        return

    deadline = time.monotonic() + max(0.0, wait)
    try:
        while True:
            try:
                _try_lock(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy(f"{path.name} is held by another process") from None
                time.sleep(poll)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                _unlock(handle)
    finally:
        handle.close()
