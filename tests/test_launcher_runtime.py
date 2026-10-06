from __future__ import annotations

import os
import ctypes
from ctypes import wintypes
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


_FAKE_RUNTIME = r'''
using System;
using System.IO;
using System.Reflection;
class FakeRuntime {
    static int Main(string[] args) {
        string name = Path.GetFileName(Assembly.GetExecutingAssembly().Location);
        bool source = name.StartsWith("python", StringComparison.OrdinalIgnoreCase);
        string stage = source ? ((args.Length > 0 && args[0] == "-c") ? "SOURCE_PREFLIGHT" : "SOURCE_APP") : "PACKAGED_APP";
        string line = stage + "|" + Directory.GetCurrentDirectory() + "|" + String.Join(" ", args);
        File.AppendAllText(Environment.GetEnvironmentVariable("LSA_FAKE_TRACE"), line + Environment.NewLine);
        int result;
        if (!Int32.TryParse(Environment.GetEnvironmentVariable("LSA_FAKE_" + stage + "_EXIT"), out result)) result = 0;
        int delay;
        if (Int32.TryParse(Environment.GetEnvironmentVariable("LSA_FAKE_" + stage + "_DELAY"), out delay)) System.Threading.Thread.Sleep(delay);
        if (result != 0) Console.Error.WriteLine("injected " + stage + " failure");
        File.WriteAllText(Environment.GetEnvironmentVariable("LSA_FAKE_TRACE") + "." + stage + ".done", "finished");
        return result;
    }
}
'''


@unittest.skipUnless(os.name == "nt", "Actual Windows CMD launch chain")
class LauncherRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).resolve().parents[1]
        cls.compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not cls.compiler.exists():
            cls.compiler = cls.compiler.parent.parent.parent / "Framework/v4.0.30319/csc.exe"
        if not cls.compiler.exists():
            raise unittest.SkipTest("Existing Windows C# compiler is unavailable; no installation attempted")
        cls.compilation = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.compilation.cleanup)
        directory = Path(cls.compilation.name)
        source = directory / "FakeRuntime.cs"
        source.write_text(_FAKE_RUNTIME, encoding="utf-8")
        cls.fake_exe = directory / "FakeRuntime.exe"
        subprocess.run(
            [str(cls.compiler), "/nologo", "/target:exe", "/out:" + str(cls.fake_exe), str(source)],
            check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "隔离 中文 空格 工具"
        (self.root / "scripts").mkdir(parents=True)
        self.entry = self.root / "启动本地切片助手.cmd"
        shutil.copy2(self.repo / self.entry.name, self.entry)
        shutil.copy2(self.repo / "scripts/launch_app.ps1", self.root / "scripts/launch_app.ps1")
        self.trace = self.root / "fake-trace.txt"

    def _install_source(self) -> None:
        directory = self.root / ".venv/Scripts"
        directory.mkdir(parents=True)
        for name in ("python.exe", "pythonw.exe"):
            shutil.copy2(self.fake_exe, directory / name)

    def _install_package(self) -> None:
        directory = self.root / "dist/LocalSliceAssistant"
        directory.mkdir(parents=True)
        shutil.copy2(self.fake_exe, directory / "LocalSliceAssistant.exe")

    def _run(self, *, smoke=True, failures=None, duplicate_path=False, delays=None):
        env = dict(os.environ)
        env["LSA_FAKE_TRACE"] = str(self.trace)
        if duplicate_path:
            env["PATH"] = env.get("PATH", env.get("Path", ""))
            env["Path"] = env["PATH"]
        for stage, code in (failures or {}).items():
            env[f"LSA_FAKE_{stage}_EXIT"] = str(code)
        for stage, milliseconds in (delays or {}).items():
            env[f"LSA_FAKE_{stage}_DELAY"] = str(milliseconds)
        command = f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /s /c ""{self.entry}"'
        command += ' --smoke-test"' if smoke else '"'
        result = subprocess.run(
            command, cwd=self.root.parent, env=env, input="\n", capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.log = (self.root / "tmp/startup.log").read_text(encoding="utf-8-sig")
        self.lines = self.trace.read_text(encoding="utf-8-sig").splitlines() if self.trace.exists() else []
        for line in self.lines:
            self.assertEqual(line.split("|", 2)[1], str(self.root))
        return result

    def test_smoke_uses_source_with_fixed_cwd_and_passes_argument(self) -> None:
        self._install_source()
        self._install_package()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + self.log)
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].startswith("SOURCE_APP|"))
        self.assertIn("--smoke-test", self.lines[0])
        self.assertIn("source-smoke: exit=0", self.log)

    def test_packaged_fallback_prefers_current_functional_release(self) -> None:
        self._install_package()
        directory = self.root / "dist/release-2026-10-06-yingxu-api-partial/LocalSliceAssistant"
        directory.mkdir(parents=True)
        shutil.copy2(self.fake_exe, directory / "映序.exe")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + self.log)
        self.assertIn("packaged-smoke: starting " + str(directory / "映序.exe"), self.log)

    def test_smoke_source_failure_falls_back_and_logs_both_results(self) -> None:
        self._install_source()
        self._install_package()
        result = self._run(failures={"SOURCE_APP": 17})
        self.assertEqual(result.returncode, 0, result.stdout + self.log)
        self.assertEqual([line.split("|", 1)[0] for line in self.lines], ["SOURCE_APP", "PACKAGED_APP"])
        self.assertTrue(all("--smoke-test" in line for line in self.lines))
        self.assertIn("injected SOURCE_APP failure", self.log)
        self.assertIn("packaged-smoke: exit=0", self.log)

    def test_normal_dependency_failure_falls_back_without_source_gui(self) -> None:
        self._install_source()
        self._install_package()
        result = self._run(smoke=False, failures={"SOURCE_PREFLIGHT": 7})
        self.assertEqual(result.returncode, 0, result.stdout + self.log)
        self.assertEqual([line.split("|", 1)[0] for line in self.lines], ["SOURCE_PREFLIGHT", "PACKAGED_APP"])
        self.assertIn("source-preflight: exit=7", self.log)

    def test_normal_source_preflight_then_launch_accepts_duplicate_path_environment(self) -> None:
        self._install_source()
        self._install_package()
        result = self._run(smoke=False, duplicate_path=True)
        self.assertEqual(result.returncode, 0, result.stdout + self.log)
        self.assertEqual([line.split("|", 1)[0] for line in self.lines], ["SOURCE_PREFLIGHT", "SOURCE_APP"])
        self.assertTrue(all("--smoke-test" not in line for line in self.lines))
        self.assertIn("source-app: exit=0", self.log)

    def test_existing_but_invalid_interpreter_falls_back(self) -> None:
        self._install_source()
        self._install_package()
        (self.root / ".venv/Scripts/python.exe").write_bytes(b"not an executable")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + self.log)
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].startswith("PACKAGED_APP|"))
        self.assertIn("source-smoke: launch failed", self.log)

    def test_detached_app_can_finish_after_launcher_returns(self) -> None:
        self._install_source()
        result = self._run(smoke=False, delays={"SOURCE_APP": 1500})
        self.assertEqual(result.returncode, 0, result.stdout + self.log)
        self.assertIn("source-app: running PID", self.log)
        completed = Path(str(self.trace) + ".SOURCE_APP.done")
        deadline = time.monotonic() + 3
        while not completed.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(completed.exists(), "Only this test's fake runtime should finish independently")
        # The marker is written just before FakeRuntime exits. Windows still
        # holds an executable-file lock until process teardown completes, so
        # wait for the exact child before TemporaryDirectory removes its copy.
        match = re.search(r"source-app: running PID (\d+)", self.log)
        self.assertIsNotNone(match)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x00100000, False, int(match.group(1)))
        if handle:
            try:
                self.assertEqual(kernel32.WaitForSingleObject(handle, 5000), 0)
            finally:
                kernel32.CloseHandle(handle)

    def test_normal_source_early_failure_falls_back(self) -> None:
        self._install_source()
        self._install_package()
        result = self._run(smoke=False, failures={"SOURCE_APP": 9})
        self.assertEqual(result.returncode, 0, result.stdout + self.log)
        self.assertEqual([line.split("|", 1)[0] for line in self.lines], ["SOURCE_PREFLIGHT", "SOURCE_APP", "PACKAGED_APP"])
        self.assertIn("source-app: exit=9", self.log)

    def test_missing_source_uses_package(self) -> None:
        self._install_package()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + self.log)
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].startswith("PACKAGED_APP|"))
        self.assertIn("Source runtime is incomplete", self.log)

    def test_both_fail_return_nonzero_and_visible_diagnostic_without_pause(self) -> None:
        self._install_source()
        self._install_package()
        result = self._run(failures={"SOURCE_APP": 17, "PACKAGED_APP": 23})
        self.assertEqual(result.returncode, 23, result.stdout + self.log)
        self.assertIn("Startup failed.", result.stdout)
        self.assertIn("All available launch paths failed", self.log)
        self.assertIn("packaged-smoke: exit=23", self.log)
        self.assertNotIn("Press any key", result.stdout)

    def test_no_runtime_returns_error_and_creates_log(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 1, result.stdout + self.log)
        self.assertIn("Packaged application was not found", self.log)
        self.assertIn("Startup failed.", result.stdout)


if __name__ == "__main__":
    unittest.main()
