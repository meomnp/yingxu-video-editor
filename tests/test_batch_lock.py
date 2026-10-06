import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from local_slice_assistant.batch_lock import batch_execution_lock, _process_running


class BatchLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.marker = self.root / "production.lock"

    def test_live_or_unknown_legacy_owner_is_never_removed(self):
        for value in (str(os.getpid()), "unknown worker", "0", "true"):
            self.marker.write_text(value)
            with self.assertRaisesRegex(ValueError, "执行锁"):
                with batch_execution_lock(self.root):
                    self.fail("must not acquire an uncertain lock")
            self.assertEqual(self.marker.read_text(), value)

    def test_denied_process_query_is_not_treated_as_dead(self):
        self.marker.write_text("123")
        with patch("local_slice_assistant.batch_lock._process_running", return_value=None):
            with self.assertRaises(ValueError):
                with batch_execution_lock(self.root):
                    pass
        self.assertEqual(self.marker.read_text(), "123")

    def test_exception_releases_only_our_marker_and_allows_next_job(self):
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with batch_execution_lock(self.root):
                self.assertTrue(_process_running(os.getpid()))
                raise RuntimeError("fixture")
        self.assertFalse(self.marker.exists())
        with batch_execution_lock(self.root):
            self.assertTrue(self.marker.exists())

    def test_second_window_blocked_then_crashed_owner_recovered(self):
        code = ("import sys,time\nfrom local_slice_assistant.batch_lock import batch_execution_lock\n"
                "with batch_execution_lock(sys.argv[1]):\n print('locked',flush=True)\n time.sleep(30)\n")
        # Windows venv python.exe is a launcher: terminating it alone can leave
        # its real Python child alive. Use the actual interpreter for this test.
        process = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", code, str(self.root)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        queue = Queue()
        reader = Thread(target=lambda: queue.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            self.assertEqual(queue.get(timeout=8).strip(), "locked")
            old = self.marker.read_bytes()
            self.assertEqual(json.loads(old)["pid"], process.pid)
            with self.assertRaisesRegex(ValueError, "执行锁"):
                with batch_execution_lock(self.root):
                    self.fail("two simultaneous owners")
            self.assertEqual(self.marker.read_bytes(), old)
            # Terminate only this finite synthetic worker, not a user's process.
            process.terminate()
            process.wait(timeout=5)
            self.assertFalse(_process_running(process.pid))
            messages = []
            with batch_execution_lock(self.root, messages.append):
                self.assertNotEqual(self.marker.read_bytes(), old)
                self.assertEqual(json.loads(self.marker.read_text())["pid"], os.getpid())
            self.assertFalse(self.marker.exists())
            self.assertTrue(any("恢复原批次" in text for text in messages))
            self.assertTrue((self.root / "production.guard").is_file())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            process.stdout.close()
            process.stderr.close()
