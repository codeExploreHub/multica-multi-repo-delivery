"""Rewrite an sdist tarball with deterministic archive metadata."""

from __future__ import annotations

import argparse
from copy import copy
import gzip
from io import BytesIO
import os
from pathlib import Path
import tarfile
import tempfile


def normalize_sdist(path: Path, epoch: int) -> None:
    archive = Path(path)
    if epoch < 0 or not archive.name.endswith(".tar.gz"):
        raise ValueError("normalization requires a .tar.gz path and non-negative epoch")
    with tarfile.open(archive, "r:gz") as source:
        records = []
        for member in sorted(source.getmembers(), key=lambda item: item.name):
            payload = source.extractfile(member).read() if member.isfile() else None
            records.append((copy(member), payload))

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive.name}.",
        dir=archive.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw,
                mtime=epoch,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as target:
                    for member, payload in records:
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        member.mtime = epoch
                        member.pax_headers = {}
                        target.addfile(
                            member,
                            BytesIO(payload) if payload is not None else None,
                        )
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--epoch", required=True, type=int)
    arguments = parser.parse_args()
    normalize_sdist(arguments.archive, arguments.epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
