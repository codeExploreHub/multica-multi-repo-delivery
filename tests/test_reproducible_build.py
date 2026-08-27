from io import BytesIO
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest

from tools.normalize_sdist import normalize_sdist


class ReproducibleBuildTests(unittest.TestCase):
    def test_sdist_normalization_removes_archive_metadata_variance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archives = (root / "first.tar.gz", root / "second.tar.gz")
            for index, archive in enumerate(archives):
                with tarfile.open(archive, "w:gz") as target:
                    member = tarfile.TarInfo("package/value.txt")
                    member.size = 5
                    member.mtime = 100 + index
                    member.uid = index + 1
                    member.uname = f"user-{index}"
                    target.addfile(member, BytesIO(b"value"))

                normalize_sdist(archive, 1787836800)

            self.assertEqual(archives[0].read_bytes(), archives[1].read_bytes())


if __name__ == "__main__":
    unittest.main()
