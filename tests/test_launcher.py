from pathlib import Path
import unittest


class LauncherTests(unittest.TestCase):
    def test_windows_entry_has_consistent_crlf_and_no_bom(self):
        root = Path(__file__).resolve().parents[1]
        data = (root / "启动本地切片助手.cmd").read_bytes()
        self.assertTrue(data.startswith(b"@echo off\r\n"))
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertIn(b"startup.log", data)
