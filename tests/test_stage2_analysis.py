from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from local_slice_assistant.analysis import (
    _capture_process,
    analyze_junction,
    audio_buckets,
    neighboring_frame_time,
)
from local_slice_assistant.analysis_cache import AnalysisCache
from local_slice_assistant.errors import AnalysisCancelled
from local_slice_assistant.exporter import ExportSettings
from local_slice_assistant.ffmpeg import ffmpeg_binary, probe_media
from local_slice_assistant.manifest import import_manifest
from local_slice_assistant.preview import create_junction_preview
from local_slice_assistant.resources import ResourceMeter, policy_for_mode, snapshot
from local_slice_assistant.transcripts import (
    TranscriptCue,
    load_srt,
    load_transcriber_markdown,
)


class Stage2AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("未找到 FFmpeg")
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name) / "基础 分析夹具"
        cls.root.mkdir()
        cls.source = cls.root / "片段.mp4"
        command = [
            ffmpeg_binary(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x0000ff:s=160x90:r=24:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-filter_complex",
            (
                "[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=0xff0000:t=fill:"
                "enable='gte(t,2)'[v];"
                "[1:a]volume=0:enable='gte(t,2)'[a]"
            ),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(cls.source),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if completed.returncode:
            raise RuntimeError(completed.stderr)
        probe = probe_media(cls.source)
        manifest = {
            "schema_version": 1,
            "example_only": False,
            "drama": "基础分析",
            "sources": [
                {
                    "file": "片段.mp4",
                    "episode": 1,
                    "expected_duration_ms": probe.duration_us // 1_000,
                }
            ],
            "cuts": [
                {
                    "title": "接缝",
                    "segments": [
                        {"file": "片段.mp4", "in_ms": 0, "out_ms": 2500},
                        {"file": "片段.mp4", "in_ms": 1500, "out_ms": 4000},
                    ],
                }
            ],
        }
        manifest_path = cls.root / "剪辑清单.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        cls.document = import_manifest(manifest_path, cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_local_candidates_cache_and_real_frame_neighbors(self) -> None:
        document = self.document
        cut = document.active_cut
        revision_before = document.revision
        cues = [
            TranscriptCue("片段.mp4", 500_000, 2_100_000, "左侧最后一句"),
            TranscriptCue("片段.mp4", 1_700_000, 2_200_000, "右侧第一句"),
        ]
        cache = AnalysisCache(document.media_root, limit_bytes=2 * 1024 * 1024)
        analysis = analyze_junction(
            document,
            cut_id=cut.id,
            junction_index=0,
            transcript_cues=cues,
            policy=policy_for_mode("saver"),
            cache=cache,
        )
        self.assertFalse(analysis.from_cache)
        self.assertEqual(analysis.revision, revision_before)
        self.assertEqual(analysis.left_segment_id, cut.segments[0].id)
        self.assertEqual(analysis.right_segment_id, cut.segments[1].id)
        self.assertTrue(
            any(candidate.kind == "transcript" for candidate in analysis.candidates)
        )
        self.assertTrue(
            any(candidate.kind == "audio" for candidate in analysis.candidates)
        )
        self.assertTrue(
            any(candidate.kind == "scene" for candidate in analysis.candidates)
        )
        self.assertEqual(document.revision, revision_before)
        self.assertEqual(document.total_duration_us(), 5_000_000)

        cached = analyze_junction(
            document,
            cut_id=cut.id,
            junction_index=0,
            transcript_cues=cues,
            policy=policy_for_mode("saver"),
            cache=cache,
        )
        self.assertTrue(cached.from_cache)
        self.assertEqual(len(cached.candidates), len(analysis.candidates))

        previous = neighboring_frame_time(
            document, cut.segments[0], boundary="out", direction=-1
        )
        following = neighboring_frame_time(
            document, cut.segments[0], boundary="out", direction=1
        )
        self.assertLess(previous, cut.segments[0].out_us)
        self.assertGreater(following, cut.segments[0].out_us)

    def test_transcript_calibration_is_undoable_and_formats_are_supported(self) -> None:
        document = self.document
        cue = TranscriptCue("片段.mp4", 500_000, 2_100_000, "原台词")
        from local_slice_assistant.transcripts import cue_key

        key = cue_key(cue)
        revision = document.revision
        document.set_transcript_override(key, "人工校正台词")
        self.assertEqual(document.transcript_overrides[key], "人工校正台词")
        self.assertTrue(document.undo())
        self.assertNotIn(key, document.transcript_overrides)
        self.assertTrue(document.redo())
        self.assertEqual(document.transcript_overrides[key], "人工校正台词")
        self.assertGreater(document.revision, revision)

        srt = self.root / "片段.srt"
        srt.write_text(
            "1\n00:00:00,500 --> 00:00:02,100\nSRT 台词\n",
            encoding="utf-8",
        )
        self.assertEqual(load_srt(srt, source_file="片段.mp4")[0].text, "SRT 台词")
        markdown = self.root / "片段.md"
        markdown.write_text(
            "- 源文件：\x60片段.mp4\x60\n\n- **00:00:00.500–00:00:02.100** MD 台词\n",
            encoding="utf-8",
        )
        self.assertEqual(load_transcriber_markdown(markdown)[0].text, "MD 台词")

    def test_no_audio_is_evidence_absent_not_an_auto_delete_signal(self) -> None:
        self.assertEqual(
            audio_buckets(
                self.source,
                start_us=0,
                end_us=1_000_000,
                has_audio=False,
            ),
            [],
        )

    def test_ten_local_runs_have_bounded_cache_and_no_parent_growth(self) -> None:
        document = self.document
        cut = document.active_cut
        before = snapshot()
        meter = ResourceMeter()
        for _index in range(10):
            result = analyze_junction(
                document,
                cut_id=cut.id,
                junction_index=0,
                transcript_cues=(),
                policy=policy_for_mode("saver"),
                cache=None,
                resource_meter=meter,
            )
            self.assertEqual(result.revision, document.revision)
        measurement = meter.finish()
        self.assertIsNotNone(measurement.peak_process_working_set_bytes)
        self.assertIn("GPU", measurement.gpu_note)
        after = snapshot()
        if (
            before.process_working_set_bytes is not None
            and after.process_working_set_bytes is not None
        ):
            self.assertLessEqual(
                after.process_working_set_bytes,
                before.process_working_set_bytes + 192 * 1024 * 1024,
            )

        cache = AnalysisCache(document.media_root, limit_bytes=2_048)
        for index in range(3):
            cache.put(cache.key_for({"entry": index}), {"payload": "x" * 1_500})
        total = sum(
            item.stat().st_size
            for item in cache.root.glob("*.json")
            if item.is_file()
        )
        self.assertLessEqual(total, 2_048)

    def test_cancelled_local_child_is_terminated(self) -> None:
        cancel = threading.Event()
        errors: list[Exception] = []

        def run() -> None:
            try:
                _capture_process(
                    [
                        ffmpeg_binary(),
                        "-hide_banner",
                        "-nostdin",
                        "-v",
                        "error",
                        "-re",
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=8000:cl=mono",
                        "-t",
                        "10",
                        "-f",
                        "s16le",
                        "-",
                    ],
                    timeout=20,
                    cancel_event=cancel,
                    failure_message="测试读取失败。",
                )
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.25)
        cancel.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AnalysisCancelled)

    def test_gui_analysis_requires_explicit_candidate_adoption(self) -> None:
        from PySide6.QtWidgets import QApplication

        from local_slice_assistant.gui import MainWindow
        from local_slice_assistant.models import ProjectDocument

        app = QApplication.instance() or QApplication([])
        document = ProjectDocument.from_dict(self.document.to_dict())
        window = MainWindow()
        try:
            window.document = document
            window.refresh()
            window.segment_table.selectRow(0)
            window._transcript_cues = [
                TranscriptCue("片段.mp4", 500_000, 2_100_000, "待校正左侧"),
                TranscriptCue("片段.mp4", 1_700_000, 2_200_000, "待校正右侧"),
            ]
            revision = document.revision
            window.analyze_selected_junction()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and window._active_worker is not None:
                app.processEvents()
                time.sleep(0.01)
            app.processEvents()
            self.assertIsNotNone(window._analysis)
            self.assertEqual(document.revision, revision)
            self.assertGreater(window.candidate_list.count(), 0)

            for index in range(window.candidate_list.count()):
                item = window.candidate_list.item(index)
                candidate = item.data(256)
                if candidate.proposed_us != candidate.current_us:
                    item.setSelected(True)
                    break
            window.apply_selected_candidates()
            self.assertGreater(document.revision, revision)
        finally:
            window.close()
            app.processEvents()

    def test_saver_proxy_stays_in_bounded_project_cache(self) -> None:
        from local_slice_assistant.models import ProjectDocument

        document = ProjectDocument.from_dict(self.document.to_dict())
        source = document.source_for("片段.mp4")
        source.width = 1_920
        source.height = 1_080
        cache_limit = 2 * 1024 * 1024
        result = create_junction_preview(
            document,
            cut_id=document.active_cut.id,
            junction_index=0,
            margin_ms=300,
            settings=ExportSettings(width=720, height=404, preset="veryfast"),
            cache_limit_bytes=cache_limit,
            cache_directory="proxies",
        )
        self.assertEqual(result.output_path.parent.name, "proxies")
        self.assertTrue(result.output_path.exists())
        cache_root = Path(document.media_root) / ".local_slice_assistant"
        total = sum(
            item.stat().st_size
            for folder in ("analysis_cache", "previews", "proxies")
            for item in (cache_root / folder).glob("*")
            if item.is_file()
        )
        self.assertLessEqual(total, cache_limit)
