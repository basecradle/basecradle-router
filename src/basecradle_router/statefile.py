"""The daemon's one way of writing a state file a reader can never see torn.

Two documents live in the state dir, the evidence ledger (:mod:`basecradle_router.evidence`)
and the waiting retries (:mod:`basecradle_router.retry`), and both are written the same way:
a temp file in the document's own directory, then ``os.replace``, so the swap is a
same-filesystem rename and a reader sees the whole previous document or the whole new one.

**Whatever creates, cleans up** (basecradle-router#281). A write's one by-product is its
temp, and the writer unlinks it on any failure it survives. A write killed between
``mkstemp`` and the swap leaves the temp where no handler reaches it, so each document's owner
sweeps its own temps when it is constructed, the one moment none of them can be live. Each
document has its own temp prefix, so a sweep can only ever remove what its own writer made,
and never a file beside it.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress

TEMP_SUFFIX = ".tmp"


def directory_of(path: str) -> str:
    """The directory a document and its temps live in."""
    return os.path.dirname(path) or "."


def atomic_write(path: str, text: str, *, prefix: str, mode: int = 0o644) -> None:
    """Replace ``path`` with ``text`` in one step a concurrent reader cannot see torn.

    Deliberately **no fsync**: atomicity is what a reader needs and ``os.replace`` gives
    it, and every caller's fail-direction for a write lost to a power cut is already safe.
    ``mkstemp`` creates the temp ``0600``, so ``mode`` is set before the swap.
    """
    fd, temp = tempfile.mkstemp(dir=directory_of(path), prefix=prefix, suffix=TEMP_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        # Never leave a stray temp behind on a failed write: the state dir would fill with
        # them, one per failed write.
        with suppress(OSError):
            os.unlink(temp)
        raise


def remove_orphaned_temps(path: str, *, prefix: str) -> list[str]:
    """Remove the temps ``prefix``'s writer left beside ``path``; return their names.

    Only regular files carrying the writer's own temp name are touched: never the document
    itself, a symlink, or anything else in the directory. A directory that cannot be
    listed, or a temp that cannot be unlinked, is passed over quietly: the first write
    reports a directory this process cannot write, exactly once.
    """
    directory = directory_of(path)
    document = os.path.basename(path)
    try:
        with os.scandir(directory) as entries:
            orphans = sorted(
                entry.name
                for entry in entries
                if entry.name.startswith(prefix)
                and entry.name.endswith(TEMP_SUFFIX)
                and entry.name != document
                and entry.is_file(follow_symlinks=False)
            )
    except OSError:
        return []
    removed = []
    for name in orphans:
        with suppress(OSError):
            os.unlink(os.path.join(directory, name))
            removed.append(name)
    return removed
