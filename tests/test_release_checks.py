import importlib.util
import io
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
import zipfile


spec = importlib.util.spec_from_file_location(
    "check_release", Path(__file__).resolve().parents[1] / "scripts/check_release.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class ReleaseChecksTest(unittest.TestCase):
    def test_private_paths_traversal_and_credential_without_value_in_error(self):
        for name in ("/outside", "../outside", r"C:\outside", "docs/../LICENSE", ".git/config",
                     "example/workspace.sqlite3-wal", "secrets/id_ed25519", "state/console-token",
                     "artifacts/objects/" + "a" * 64):
            with self.subTest(name=name), self.assertRaises(ValueError):
                checks.verify_path(name)
        synthetic = b"ghp_" + b"X" * 36
        with self.assertRaisesRegex(ValueError, "value withheld") as error:
            checks.check_contents(io.BytesIO(synthetic), len(synthetic), "test-input.txt")
        self.assertNotIn(synthetic.decode(), str(error.exception))

    def test_archive_links_and_missing_files(self):
        with tempfile.TemporaryDirectory() as temp:
            wheel = Path(temp) / "clauseweft-0.0.1-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                link = zipfile.ZipInfo("link")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o700) << 16
                archive.writestr(link, "target")
            with self.assertRaisesRegex(ValueError, "link"):
                checks.verify_archive(wheel, "0.0.1")
            tar = Path(temp) / "clauseweft-0.0.1.tar.gz"
            for mode in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.REGTYPE):
                with tarfile.open(tar, "w:gz") as archive:
                    member = tarfile.TarInfo("clauseweft-0.0.1/example")
                    member.type = mode
                    if mode != tarfile.REGTYPE:
                        member.linkname = "/outside"
                    archive.addfile(member)
                with self.assertRaisesRegex(ValueError, "missing|link"):
                    checks.verify_archive(tar, "0.0.1")
