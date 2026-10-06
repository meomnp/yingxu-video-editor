from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_slice_assistant.errors import ProjectSaveError
from local_slice_assistant.models import Cut, ProjectDocument, Segment, SourceInfo
from local_slice_assistant.project_store import load_project, save_project


class ProjectRecoverySafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.target = self.root / "合成工程.localcut.json"
        self.recovery = self.target.with_name(f"{self.target.name}.recovery")
        source = SourceInfo(
            relative_path="synthetic.mp4", episode=1,
            expected_duration_us=2_000_000, duration_us=2_000_000,
            size=1, mtime_ns=1, quick_hash="synthetic", has_audio=False,
            fps_num=25, fps_den=1, width=160, height=90,
        )
        # This fixture contains metadata only; no video is created or inspected.
        self.document = ProjectDocument(
            media_root=str(self.root), drama="旧内容", original_manifest={},
            sources={source.relative_path: source},
            cuts=[Cut(title="片段", segments=[Segment(source.relative_path, 0, 1_000_000, "片段")])],
        )

    def _updated(self) -> ProjectDocument:
        document = ProjectDocument.from_dict(self.document.to_dict())
        document.drama = "新内容"
        document.revision += 1
        return document

    def _assert_no_temporary_files(self) -> None:
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def _read_recovery(self) -> ProjectDocument:
        return ProjectDocument.from_dict(json.loads(self.recovery.read_text(encoding="utf-8")))

    def test_normal_save_keeps_previous_project_as_recovery(self) -> None:
        save_project(self.document, self.target)
        original = self.target.read_bytes()
        save_project(self._updated(), self.target)
        self.assertEqual(self.recovery.read_bytes(), original)
        loaded = load_project(self.target, validate_sources=False)
        self.assertEqual(loaded.document.drama, "新内容")
        self.assertFalse(loaded.recovered_from_backup)
        self._assert_no_temporary_files()

    def test_invalid_primary_never_replaces_valid_recovery(self) -> None:
        invalid_schema = self.document.to_dict()
        invalid_schema["schema_version"] = 999
        corruptions = (
            b"{", b"{}", b"[]", b"\xff\xfe",
            json.dumps(invalid_schema).encode("utf-8"),
        )
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                save_project(self.document, self.target)
                original = self.target.read_bytes()
                self.recovery.write_bytes(original)
                self.target.write_bytes(corrupt)
                loaded = load_project(self.target, validate_sources=False)
                self.assertTrue(loaded.recovered_from_backup)
                self.assertEqual(loaded.document.drama, "旧内容")
                save_project(self._updated(), self.target)
                self.assertEqual(self.recovery.read_bytes(), original)
                self.assertEqual(load_project(self.target, validate_sources=False).document.drama, "新内容")
                self._assert_no_temporary_files()

    def test_missing_primary_preserves_existing_recovery(self) -> None:
        save_project(self.document, self.target)
        original = self.recovery.read_bytes()
        self.target.unlink()
        save_project(self._updated(), self.target)
        self.assertEqual(self.recovery.read_bytes(), original)
        self.assertEqual(load_project(self.target, validate_sources=False).document.drama, "新内容")

    def test_both_invalid_files_get_a_readable_new_recovery(self) -> None:
        self.target.write_bytes(b"{")
        self.recovery.write_bytes(b"{")
        save_project(self._updated(), self.target)
        self.assertEqual(self._read_recovery().drama, "新内容")
        self.assertEqual(load_project(self.target, validate_sources=False).document.drama, "新内容")

    def test_partial_backup_copy_failure_preserves_both_existing_files(self) -> None:
        save_project(self.document, self.target)
        primary_before = self.target.read_bytes()
        recovery_before = self.recovery.read_bytes()

        def partial_copy(_source: Path, destination: Path) -> None:
            Path(destination).write_bytes(b"{")
            raise PermissionError("injected partial backup copy failure")

        with patch("local_slice_assistant.project_store.shutil.copy2", side_effect=partial_copy):
            with self.assertRaises(ProjectSaveError):
                save_project(self._updated(), self.target)
        self.assertEqual(self.target.read_bytes(), primary_before)
        self.assertEqual(self.recovery.read_bytes(), recovery_before)
        self.assertEqual(load_project(self.target, validate_sources=False).document.drama, "旧内容")
        self.assertEqual(self._read_recovery().drama, "旧内容")
        self._assert_no_temporary_files()

    def test_backup_replace_failure_preserves_both_existing_files(self) -> None:
        save_project(self.document, self.target)
        primary_before = self.target.read_bytes()
        recovery_before = self.recovery.read_bytes()
        replace = os.replace

        def fail_backup_replace(source: Path, destination: Path) -> None:
            if Path(destination) == self.recovery:
                raise OSError("injected backup replacement failure")
            replace(source, destination)

        with patch("local_slice_assistant.project_store.os.replace", side_effect=fail_backup_replace):
            with self.assertRaises(ProjectSaveError):
                save_project(self._updated(), self.target)
        self.assertEqual(self.target.read_bytes(), primary_before)
        self.assertEqual(self.recovery.read_bytes(), recovery_before)
        self.assertEqual(load_project(self.target, validate_sources=False).document.drama, "旧内容")
        self.assertEqual(self._read_recovery().drama, "旧内容")
        self._assert_no_temporary_files()

    def test_primary_replace_failure_keeps_a_readable_previous_project(self) -> None:
        replace = os.replace

        def fail_primary_replace(source: Path, destination: Path) -> None:
            if Path(destination) == self.target:
                raise PermissionError("injected primary replacement failure")
            replace(source, destination)

        for primary_state in ("valid", "corrupt", "missing"):
            with self.subTest(primary_state=primary_state):
                save_project(self.document, self.target)
                recovery_before = self.recovery.read_bytes()
                if primary_state == "corrupt":
                    self.target.write_bytes(b"{")
                elif primary_state == "missing":
                    self.target.unlink()
                with patch("local_slice_assistant.project_store.os.replace", side_effect=fail_primary_replace):
                    with self.assertRaises(ProjectSaveError):
                        save_project(self._updated(), self.target)
                self.assertEqual(self.recovery.read_bytes(), recovery_before)
                loaded = load_project(self.target, validate_sources=False)
                self.assertEqual(loaded.document.drama, "旧内容")
                self.assertEqual(loaded.recovered_from_backup, primary_state != "valid")
                self._assert_no_temporary_files()

    def test_first_primary_replace_failure_still_leaves_readable_recovery(self) -> None:
        replace = os.replace

        def fail_primary_replace(source: Path, destination: Path) -> None:
            if Path(destination) == self.target:
                raise OSError("injected initial primary replacement failure")
            replace(source, destination)

        with patch("local_slice_assistant.project_store.os.replace", side_effect=fail_primary_replace):
            with self.assertRaises(ProjectSaveError):
                save_project(self.document, self.target)
        self.assertFalse(self.target.exists())
        loaded = load_project(self.target, validate_sources=False)
        self.assertTrue(loaded.recovered_from_backup)
        self.assertEqual(loaded.document.drama, "旧内容")
        self._assert_no_temporary_files()


if __name__ == "__main__":
    unittest.main()
