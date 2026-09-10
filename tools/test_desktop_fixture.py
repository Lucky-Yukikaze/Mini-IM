"""Verify desktop snapshots while download artifacts change on disk."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop_fixture import DesktopFixture


class DesktopSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="miniim-snapshot-")
        self.root = Path(self.temporary.name)
        self.fixture = DesktopFixture(None, self.root, self.root)
        self.files = self.root / "desktop files"
        self.files.mkdir()
        self.source = self.files / "source.bin"
        self.source.write_bytes(b"verified source")

    def tearDown(self):
        self.fixture.db.close()
        self.fixture.server_log.close()
        self.temporary.cleanup()

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_locked_temporary_file_does_not_hide_readable_artifacts(self):
        target = self.files / "download.bin.temporary"
        target.write_bytes(b"pending publication")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(target), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        try:
            with self.assertRaises(PermissionError):
                target.read_bytes()
            state = self.fixture.snapshot()
            self.assertNotIn(target.name, state["artifacts"])
            self.assertEqual(state["artifactErrors"][target.name], "PermissionError")
            self.assertEqual(state["artifacts"][self.source.name]["sha256"],
                hashlib.sha256(self.source.read_bytes()).hexdigest())
        finally:
            self.assertTrue(kernel.CloseHandle(handle))
        state = self.fixture.snapshot()
        self.assertEqual(state["artifactErrors"], {})
        self.assertEqual(state["artifacts"][target.name]["sha256"],
            hashlib.sha256(target.read_bytes()).hexdigest())

    def test_file_removed_between_enumeration_and_read_is_reported(self):
        target = self.files / "download.bin.temporary"
        target.write_bytes(b"pending publication")
        original = Path.read_bytes
        def remove_before_read(path):
            if path == target:
                path.unlink()
            return original(path)
        with patch.object(Path, "read_bytes", remove_before_read):
            state = self.fixture.snapshot()
        self.assertNotIn(target.name, state["artifacts"])
        self.assertEqual(state["artifactErrors"][target.name], "FileNotFoundError")
        self.assertEqual(state["artifacts"][self.source.name]["size"], len(b"verified source"))


if __name__ == "__main__":
    unittest.main()
