from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_slice_assistant.errors import ManifestValidationError
from local_slice_assistant.exporter import export_cut
from local_slice_assistant.manifest import create_project_from_videos, import_manifest, import_planned_manifest
from local_slice_assistant.paths import quick_hash
from local_slice_assistant.planning_package import build_web_planning_package, write_web_planning_package
from local_slice_assistant.project_store import save_project, load_project
from local_slice_assistant.transcripts import load_transcript_files, map_cues_to_timeline
from tests.helpers import make_empty_sources, patched_probe, standard_manifest, write_manifest
from tests.test_stage1_media_integration import _make_colored_media, _pixel_at, _audio_at


class PlannedImportTests(unittest.TestCase):
    def test_current_package_id_required_and_persists_in_project(self):
        with tempfile.TemporaryDirectory() as raw_root, patched_probe():
            root = Path(raw_root)
            make_empty_sources(root)
            path = write_manifest(root)
            before = import_manifest(path, root)
            before.planning_context = {"package_id": "current-package"}
            before = load_project(save_project(before, root / "context.localcut.json")).document
            for identifier in (None, "older-package"):
                raw = standard_manifest()
                if identifier:
                    raw["planning_package_id"] = identifier
                write_manifest(root, raw)
                with patch("local_slice_assistant.manifest.probe_media") as probe:
                    with self.assertRaisesRegex(ManifestValidationError, "不对应当前任务包"):
                        import_planned_manifest(path, before)
                    probe.assert_not_called()
            raw["planning_package_id"] = "current-package"
            write_manifest(root, raw)
            imported = import_planned_manifest(path, before)
            self.assertEqual(imported.planning_context["package_id"], "current-package")

    def test_context_and_unused_sources_survive_without_changing_original(self):
        with tempfile.TemporaryDirectory() as raw_root, patched_probe():
            root = Path(raw_root)
            make_empty_sources(root)
            path = write_manifest(root)
            before = import_manifest(path, root)
            before.set_transcript_files(["19.srt", "16.srt"], bindings={"19.srt": "19.mp4", "16.srt": "16.mp4"})
            before.transcript_overrides = {"19.mp4|0|1000": "已校正"}
            before.planning_context = {"objective": "连贯切片", "package_path": "任务包.json"}
            original = copy.deepcopy(before.to_dict())
            raw = standard_manifest()
            raw["sources"] = [raw["sources"][0]]
            raw["cuts"][0]["segments"] = [raw["cuts"][0]["segments"][0]]
            write_manifest(root, raw)
            after = import_planned_manifest(path, before)
            self.assertEqual(after.transcript_bindings, before.transcript_bindings)
            self.assertEqual(after.transcript_overrides, before.transcript_overrides)
            self.assertEqual(set(after.sources), set(before.sources))
            self.assertEqual(before.to_dict(), original)
            self.assertEqual(after.planning_context["objective"], "连贯切片")
            after.sources["19.mp4"].episode = 99
            self.assertEqual(before.sources["19.mp4"].episode, 19)

    def test_different_batch_is_rejected_before_reading_unknown_media(self):
        with tempfile.TemporaryDirectory() as raw_root, patched_probe():
            root = Path(raw_root)
            make_empty_sources(root)
            path = write_manifest(root)
            before = import_manifest(path, root)
            raw = standard_manifest()
            raw["sources"][0]["file"] = "未选择.mp4"
            write_manifest(root, raw)
            with patch("local_slice_assistant.manifest.probe_media") as probe:
                with self.assertRaisesRegex(ManifestValidationError, "没有导入"):
                    import_planned_manifest(path, before)
                probe.assert_not_called()

    def test_changed_source_and_changed_drama_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw_root, patched_probe():
            root = Path(raw_root)
            make_empty_sources(root)
            path = write_manifest(root)
            before = import_manifest(path, root)
            (root / "19.mp4").write_bytes(b"changed material")
            with self.assertRaisesRegex(ManifestValidationError, "素材.*变化"):
                import_planned_manifest(path, before)
            raw = standard_manifest()
            raw["drama"] = "另外一部剧"
            write_manifest(root, raw)
            with self.assertRaisesRegex(ManifestValidationError, "剧名"):
                import_planned_manifest(path, before)


class WebWorkflowMediaTests(unittest.TestCase):
    def test_task_package_return_plan_save_reopen_and_real_export(self):
        """Deterministic simulated web response, NOT a live GPT quality test."""
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "AI 工作流 合成验收"
            root.mkdir()
            first, second = root / "第01集.mp4", root / "第02集.mp4"
            _make_colored_media(first, duration=8, color="red", fps=24, audio=True, frequency=440)
            _make_colored_media(second, duration=8, color="blue", fps=30, audio=False)
            hashes = {path.name: quick_hash(path) for path in (first, second)}
            document = create_project_from_videos([first, second])
            md, txt = root / "01.md", root / "02.txt"
            md.write_text("源文件：第01集.mp4\n00:00:01 --> 00:00:02 前因\n00:00:03 --> 00:00:04 结果", encoding="utf-8")
            txt.write_text("00:00:02 --> 00:00:03 钩子\n00:00:05 --> 00:00:06 悬念", encoding="utf-8")
            document.set_transcript_files([str(md), str(txt)], bindings={str(txt): second.name})
            cues = load_transcript_files(document.transcript_paths, {}, source_bindings=document.transcript_bindings)
            package = build_web_planning_package(document, cues)
            package_path = write_web_planning_package(package, root / "任务包.json")
            document.planning_context = {"objective": "合成重排测试", "package_path": str(package_path), "package_id": package["package_id"]}
            bounds = package["boundary_candidates"]
            pairs = [(2, 2), (0, 1), (3, 3), (2, 2)]  # B -> A -> B -> repeated B
            segments = []
            for start, end in pairs:
                segments.append({"file": bounds[start]["file"], "in_ms": bounds[start]["suggested_in_ms"],
                                 "out_ms": bounds[end]["suggested_out_ms"], "purpose": "合成验收",
                                 "first_line": cues[start].text, "last_line": cues[end].text, "original_audio": "keep"})
            response = {"schema_version": 1, "example_only": False, "drama": document.drama,
                        "planning_package_id": package["package_id"],
                        "sources": [{key: item[key] for key in ("file", "episode", "expected_duration_ms")}
                                    for item in package["source_catalog"]],
                        "cuts": [{"title": "合成重排", "segments": segments}]}
            response_path = root / "模拟网页返回.json"
            response_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
            imported = import_planned_manifest(response_path, document)
            self.assertEqual(imported.total_duration_us(), 8_400_000)
            project_path = save_project(imported, root / "粗剪.localcut.json")
            reopened = load_project(project_path).document
            self.assertEqual(reopened.planning_context, imported.planning_context)
            restored_cues = load_transcript_files(reopened.transcript_paths, reopened.transcript_overrides,
                                                  source_bindings=reopened.transcript_bindings)
            mapped = map_cues_to_timeline(reopened.active_cut, restored_cues)
            self.assertEqual([(cue.start_us, cue.end_us) for cue in mapped],
                             [(200000, 1200000), (1800000, 2800000), (3800000, 4800000),
                              (5400000, 6400000), (7000000, 8000000)])
            # Keep the one-second pause inside A rather than pruning per utterance.
            self.assertEqual(mapped[2].start_us - mapped[1].end_us, 1_000_000)
            output = export_cut(reopened)
            self.assertLessEqual(abs(output.observed_duration_us - 8_400_000), output.frame_duration_us)
            for seconds, channel in ((0.5, 2), (2.5, 0), (5.8, 2), (7.5, 2)):
                self.assertGreater(_pixel_at(output.output_path, seconds)[channel], 170)
            self.assertLess(_audio_at(output.output_path, 0.5)[1], 80)
            self.assertAlmostEqual(_audio_at(output.output_path, 2.5)[0], 440, delta=35)
            self.assertEqual(hashes, {path.name: quick_hash(path) for path in (first, second)})
