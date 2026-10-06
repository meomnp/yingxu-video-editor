from pathlib import Path
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from local_slice_assistant.errors import LocalSliceError
from local_slice_assistant.voice_bridge import run_voice_bridge


class BridgeTests(unittest.TestCase):
    def run_script(self, code, cancel=None, timeout=5):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            cli = path / "client.py"
            cli.write_text(code, encoding="utf-8")
            with patch("local_slice_assistant.voice_bridge.VOICE_PYTHON", Path(sys.executable)), patch("local_slice_assistant.voice_bridge.VOICE_CLI", cli):
                return run_voice_bridge(path / "request.json", path, "profile", lambda _: None, cancel or Event(), timeout_seconds=timeout)

    def test_success(self):
        self.run_script("import json, pathlib\np = pathlib.Path(__file__).parent / 'result.zip'\np.write_bytes(b'fixture')\nprint(json.dumps({'event':'completed','package':str(p)}))")

    def test_failure(self):
        with self.assertRaisesRegex(LocalSliceError, "rejected"):
            self.run_script("import json,sys\nprint(json.dumps({'event':'error','message':'rejected'}))\nsys.exit(2)")

    def test_timeout_only_stops_client(self):
        with self.assertRaisesRegex(LocalSliceError, "停止等待"):
            self.run_script("import time\ntime.sleep(20)", timeout=0.1)

    def test_pre_cancel(self):
        cancel = Event()
        cancel.set()
        with self.assertRaisesRegex(LocalSliceError, "尚未提交"):
            self.run_script("raise RuntimeError('must not run')", cancel)
