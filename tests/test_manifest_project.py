from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_slice_assistant.errors import ManifestValidationError
from local_slice_assistant.manifest import create_project_from_folder, create_project_from_video, import_manifest
from local_slice_assistant.project_store import (
    load_project,
    relink_project,
    save_project,
)
from local_slice_assistant.transcripts import TranscriptCue, map_cues_to_timeline

from tests.helpers import (
    SOURCE_DURATIONS_MS,
    make_empty_sources,
    patched_probe,
    standard_manifest,
    write_manifest,
)


class ManifestAndProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "剧集 素材"
        self.root.mkdir()
        make_empty_sources(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _import(self, payload: dict[str, object] | None = None):
        manifest = write_manifest(self.root, payload)
        with patched_probe():
            return import_manifest(manifest, self.root)

    def test_existing_video_opens_without_json_and_reopens_as_project(self) -> None:
        source = self.root / "19.mp4"
        with patched_probe():
            document = create_project_from_video(source)
            self.assertEqual(document.media_root, str(self.root.resolve()))
            self.assertEqual(document.drama, "19")
            self.assertEqual(len(document.active_cut.segments), 1)
            self.assertEqual(document.active_cut.segments[0].source_file, "19.mp4")
            self.assertEqual(document.active_cut.segments[0].in_us, 0)
            self.assertEqual(document.active_cut.segments[0].out_us, document.sources["19.mp4"].duration_us)
            split_at = document.active_cut.segments[0].out_us // 2
            document.split_segment(document.active_cut.id, 0, split_at)
            self.assertEqual(len(document.active_cut.segments), 2)
            self.assertEqual(document.active_cut.segments[0].out_us, split_at)
            self.assertEqual(document.active_cut.segments[1].in_us, split_at)
            self.assertEqual(document.total_duration_us(), document.sources["19.mp4"].duration_us)
            self.assertTrue(document.undo())
            project = self.root / "现成视频.localcut.json"
            save_project(document, project)
            reopened = load_project(project).document
        self.assertEqual(reopened.active_cut.segments[0].source_file, "19.mp4")

    def test_manual_folder_loads_multiple_videos_without_json(self) -> None:
        legacy_export = self.root / "映序项目" / "剧名_第001批" / "成片" / "01.mp4"
        legacy_export.parent.mkdir(parents=True)
        legacy_export.write_bytes(b"old generated video")
        with patched_probe():
            document = create_project_from_folder(self.root)
            files = [segment.source_file for segment in document.active_cut.segments]
            self.assertEqual(files, ["16.mp4", "17.mp4", "18.mp4", "19.mp4"])
            self.assertEqual(len(document.sources), 4)
            document.reorder_segments(document.active_cut.id, [
                document.active_cut.segments[index].id for index in (3, 0, 1, 2)
            ])
            self.assertEqual(document.active_cut.segments[0].source_file, "19.mp4")

    def test_repeated_segment_exact_duration_extend_undo_save_and_reopen(self) -> None:
        document = self._import()
        cut = document.active_cut
        self.assertEqual(
            [segment.source_file for segment in cut.segments],
            ["19.mp4", "16.mp4", "17.mp4", "18.mp4", "19.mp4"],
        )
        self.assertEqual(document.total_duration_us(), 195_000_000)

        document.adjust_segment_end(cut.id, 4, 1_000_000)
        self.assertEqual(document.total_duration_us(), 196_000_000)
        self.assertTrue(document.undo())
        self.assertEqual(document.total_duration_us(), 195_000_000)

        project = self.root / "阶段1.localcut.json"
        with patched_probe():
            save_project(document, project)
            reopened = load_project(project).document
        self.assertTrue(project.exists())
        self.assertTrue(project.with_name(f"{project.name}.recovery").exists())
        self.assertEqual(reopened.total_duration_us(), 195_000_000)
        self.assertEqual(reopened.revision, document.revision)

    def test_delete_restore_and_subtitle_mapping_follow_current_timeline(self) -> None:
        document = self._import()
        cut = document.active_cut
        cues = [
            TranscriptCue("16.mp4", 11_000_000, 12_000_000, "第16集台词"),
            TranscriptCue("17.mp4", 8_000_000, 10_000_000, "第17集台词"),
            TranscriptCue("18.mp4", 12_000_000, 13_000_000, "第18集台词"),
        ]
        initial = map_cues_to_timeline(cut, cues)
        self.assertEqual([item.text for item in initial], ["第16集台词", "第17集台词", "第18集台词"])
        self.assertEqual(initial[0].start_us, 9_000_000)

        document.delete_segment(cut.id, 2)
        after_delete = map_cues_to_timeline(document.active_cut, cues)
        self.assertEqual([item.text for item in after_delete], ["第16集台词", "第18集台词"])
        self.assertEqual(after_delete[1].start_us, 51_000_000)

        self.assertTrue(document.undo())
        restored = map_cues_to_timeline(document.active_cut, cues)
        self.assertEqual([item.text for item in restored], ["第16集台词", "第17集台词", "第18集台词"])

    def test_relink_after_original_file_rename(self) -> None:
        document = self._import()
        project = self.root / "重关联.localcut.json"
        with patched_probe():
            save_project(document, project)

        renamed = self.root / "19_重命名.mp4"
        (self.root / "19.mp4").rename(renamed)
        with patched_probe():
            loaded = load_project(project, validate_sources=False).document
            relink_project(loaded, self.root)
        self.assertIn("19_重命名.mp4", loaded.sources)
        self.assertEqual(loaded.active_cut.segments[0].source_file, "19_重命名.mp4")
        self.assertEqual(loaded.active_cut.segments[-1].source_file, "19_重命名.mp4")

    def test_clear_validation_errors_for_known_bad_inputs(self) -> None:
        cases: list[tuple[str, dict[str, object], str]] = []

        missing = standard_manifest()
        missing["sources"] = [
            {
                "file": "不存在.mp4",
                "episode": 1,
                "expected_duration_ms": 1_000,
            }
        ]
        missing["cuts"] = [{"title": "缺失", "segments": [{"file": "不存在.mp4", "in_ms": 0, "out_ms": 1_000}]}]
        cases.append(("missing", missing, "不存在"))

        negative = standard_manifest()
        negative["cuts"][0]["segments"][0]["in_ms"] = -1  # type: ignore[index]
        cases.append(("negative", negative, "不能小于"))

        invalid_range = standard_manifest()
        invalid_range["cuts"][0]["segments"][0]["out_ms"] = 0  # type: ignore[index]
        cases.append(("range", invalid_range, "必须大于"))

        mismatch = standard_manifest()
        mismatch["sources"][0]["expected_duration_ms"] = 1  # type: ignore[index]
        cases.append(("duration mismatch", mismatch, "时长"))

        for name, payload, phrase in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ManifestValidationError, phrase):
                    self._import(payload)

    def test_duplicate_same_name_in_different_directories_is_rejected(self) -> None:
        for folder in ("A", "B"):
            directory = self.root / folder
            directory.mkdir()
            (directory / "同名.mp4").write_bytes(folder.encode("utf-8"))
        payload = {
            "schema_version": 1,
            "example_only": False,
            "drama": "同名测试",
            "sources": [
                {"file": "A/同名.mp4", "episode": 1, "expected_duration_ms": 160_000},
                {"file": "B/同名.mp4", "episode": 2, "expected_duration_ms": 160_000},
            ],
            "cuts": [
                {
                    "title": "同名",
                    "segments": [{"file": "A/同名.mp4", "in_ms": 0, "out_ms": 1_000}],
                }
            ],
        }
        with self.assertRaisesRegex(ManifestValidationError, "同名"):
            self._import(payload)

    def test_path_escape_is_rejected_before_any_probe(self) -> None:
        payload = standard_manifest()
        payload["sources"][0]["file"] = "../19.mp4"  # type: ignore[index]
        with self.assertRaisesRegex(ManifestValidationError, "不能包含"):
            self._import(payload)

    def test_publishing_and_stage2_settings_round_trip(self) -> None:
        payload = standard_manifest()
        payload["cuts"][0]["publishing"] = {
            "kind": "video",
            "title": "准确标题",
            "body": "准确正文",
            "tags": [
                "#合成验收剧",
                "#短剧",
                "#剧情",
                "#反转",
                "#冲突",
                "#切片",
            ],
        }
        document = self._import(payload)
        self.assertEqual(document.active_cut.publishing["tags"][0], "#合成验收剧")  # type: ignore[index]
        document.set_transcript_files(
            ["D:/fixture/19.srt"], bindings={"D:/fixture/19.srt": "19.mp4"}
        )
        document.set_resource_mode("saver")
        self.assertEqual(document.resource_settings["cpu_threads"], 2)
        self.assertTrue(document.undo())
        self.assertEqual(document.resource_settings["mode"], "standard")
        self.assertTrue(document.redo())
        self.assertEqual(document.resource_settings["mode"], "saver")

        project = self.root / "阶段2.localcut.json"
        with patched_probe():
            save_project(document, project)
            reopened = load_project(project).document
        self.assertEqual(reopened.transcript_bindings["D:/fixture/19.srt"], "19.mp4")
        self.assertEqual(reopened.active_cut.publishing["body"], "准确正文")  # type: ignore[index]

        bad = standard_manifest()
        bad["cuts"][0]["publishing"] = {
            "kind": "video",
            "title": "标题",
            "body": "正文",
            "tags": ["#错误", "#短剧", "#剧情", "#反转", "#冲突", "#切片"],
        }
        with self.assertRaisesRegex(ManifestValidationError, "首标签"):
            self._import(bad)
