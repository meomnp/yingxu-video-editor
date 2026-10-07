import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prune_qt_virtualkeyboard.py"


class QtPackagePruningTests(unittest.TestCase):
    def test_removes_virtual_keyboard_binaries_plugins_and_frameworks_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "PySide6"
            (root / "QtCore.abi3.so").parent.mkdir(parents=True)
            (root / "QtCore.abi3.so").write_bytes(b"core")
            paths = [
                root / "Qt6VirtualKeyboard.dll",
                root / "plugins" / "platforminputcontexts" / "qtvirtualkeyboardplugin.dll",
                root / "Qt" / "lib" / "QtVirtualKeyboard.framework" / "QtVirtualKeyboard",
                root / "Qt" / "lib" / "QtVirtualKeyboardQml.framework" / "QtVirtualKeyboardQml",
                root / "QtWidgets.abi3.so",
            ]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")

            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Excluded GPLv3-only Qt Virtual Keyboard artifacts", result.stdout)
            self.assertTrue((root / "QtCore.abi3.so").exists())
            self.assertTrue((root / "QtWidgets.abi3.so").exists())
            for path in paths[:-1]:
                self.assertFalse(path.exists(), str(path))

    def test_refuses_non_qt_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), temp],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not look like a PySide/Qt bundle", result.stderr)


if __name__ == "__main__":
    unittest.main()
