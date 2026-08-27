"""Crash-safe local artifact writes."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


def atomic_write_new(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Create a new file without overwriting any existing path."""

    destination = Path(path)
    if destination.exists():
        raise FileExistsError(destination)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    created = True
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        created = False
    finally:
        if created:
            destination.unlink(missing_ok=True)


def atomic_replace_private(path: Path, data: bytes) -> None:
    """Atomically replace a file with a flushed mode-0600 temporary file."""

    destination = Path(path)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
