from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from local_slice_assistant.analysis import JunctionAnalysis, JunctionCandidate
from local_slice_assistant.analysis_cache import AnalysisCache
from local_slice_assistant.errors import (
    ManifestValidationError,
    VisionBackendUnavailable,
    VisionCancelled,
)
from local_slice_assistant.models import Cut, ProjectDocument, Segment, SourceInfo
from local_slice_assistant.vision import (
    DEFAULT_FRAME_BUDGET,
    MAX_FRAME_BUDGET,
    VisionAnalysis,
    VisionBackend,
    VisionSampling,
    _normalize_observation,
    _run_child,
    plan_junction_frames,
    sampling_for_mode,
)


def _document(root: Path) -> ProjectDocument:
    source = SourceInfo(
        relative_path="source.mp4",
        episode=1,
        expected_duration_us=12_000_000,
        duration_us=12_000_000,
        size=1,
        mtime_ns=1,
        quick_hash="stage3-fixture",
        has_audio=True,
        fps_num=24,
        fps_den=1,
        width=320,
        height=180,
    )
    return ProjectDocument(
        media_root=str(root),
        drama="画面夹具",
        original_manifest={},
        sources={source.relative_path: source},
        cuts=[
            Cut(
                title="接缝",
                segments=[
                    Segment("source.mp4", 1_000_000, 6_000_000, "前段"),
                    Segment("source.mp4", 6_000_000, 11_000_000, "后段"),
                ],
            )
        ],
    )


class Stage3VisionTests(unittest.TestCase):
    def test_sampling_is_ordered_bounded_and_keeps_both_sides(self) -> None:
        document = _document(Path(tempfile.gettempdir()))
        cut = document.active_cut
        standard = sampling_for_mode("standard")
        frames = plan_junction_frames(
            cut.segments[0], cut.segments[1], sampling=standard
        )
        self.assertEqual(standard.frame_budget, DEFAULT_FRAME_BUDGET)
        self.assertLessEqual(len(frames), MAX_FRAME_BUDGET)
        self.assertEqual([frame.order for frame in frames], list(range(1, len(frames) + 1)))
        self.assertTrue(all(frame.side == "left" for frame in frames[: len(frames) // 2]))
        self.assertTrue(all(frame.side == "right" for frame in frames[len(frames) // 2 :]))
        self.assertTrue(
            all(cut.segments[0].in_us <= frame.source_us < cut.segments[0].out_us for frame in frames if frame.side == "left")
        )
        self.assertTrue(
            all(cut.segments[1].in_us <= frame.source_us < cut.segments[1].out_us for frame in frames if frame.side == "right")
        )
        with self.assertRaises(ManifestValidationError):
            VisionSampling(frame_budget=MAX_FRAME_BUDGET + 1).validate()

    def test_observation_never_fabricates_a_candidate(self) -> None:
        visible, missing, recommendation, candidate, reason, undecidable = _normalize_observation(
            b"The earlier frames are blue and later frames are red. Pictures alone cannot prove an action."
        )
        self.assertIn("blue", visible)
        self.assertTrue(missing)
        self.assertEqual(recommendation, "expand_manual_review")
        self.assertIsNone(candidate)
        self.assertTrue(reason)
        self.assertIn("cannot", undecidable)

    def test_vision_cache_is_separate_and_clearable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = AnalysisCache(
                temporary, limit_bytes=4_096, namespace="vision_cache"
            )
            key = cache.key_for({"vision": 1})
            cache.put(key, {"answer": "local-only"})
            self.assertEqual(cache.get(key), {"answer": "local-only"})
            self.assertEqual(cache.root.name, "vision_cache")

    def test_missing_backend_is_an_explicit_manual_fallback(self) -> None:
        backend = VisionBackend(
            executable=Path(tempfile.gettempdir()) / "missing-vision.exe",
            model=Path(tempfile.gettempdir()) / "missing-model.gguf",
            projector=Path(tempfile.gettempdir()) / "missing-projector.gguf",
        )
        with self.assertRaises(VisionBackendUnavailable) as raised:
            backend.validate_available()
        self.assertIn("基础剪辑", str(raised.exception))

    def test_cli_exposes_non_mutating_visual_analysis(self) -> None:
        from local_slice_assistant.cli import build_parser

        args = build_parser().parse_args(
            [
                "analyze-vision",
                "--project",
                "fixture.localcut.json",
                "--cut",
                "cut-1",
                "--junction",
                "0",
            ]
        )
        self.assertEqual(args.command, "analyze-vision")
        self.assertIsNone(args.mode)

    def test_cancelled_child_is_stopped(self) -> None:
        cancel = threading.Event()
        errors: list[Exception] = []

        def run() -> None:
            try:
                _run_child(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    timeout_seconds=20,
                    cancel_event=cancel,
                    error_message="测试视觉进程失败。",
                )
            except Exception as exc:  # expected cancellation path
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.25)
        cancel.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], VisionCancelled)

    def test_gui_displays_visual_evidence_without_auto_edit(self) -> None:
        from PySide6.QtWidgets import QApplication

        from local_slice_assistant.gui import MainWindow

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temporary:
            document = _document(Path(temporary))
            cut = document.active_cut
            candidate = JunctionCandidate(
                id="stage3-candidate",
                side="left_out",
                segment_id=cut.segments[0].id,
                source_file="source.mp4",
                current_us=cut.segments[0].out_us,
                proposed_us=cut.segments[0].out_us - 100_000,
                kind="scene",
                reason="基础候选",
                evidence="fixture",
            )
            base = JunctionAnalysis(
                revision=document.revision,
                cut_id=cut.id,
                junction_index=0,
                left_segment_id=cut.segments[0].id,
                right_segment_id=cut.segments[1].id,
                candidates=(candidate,),
            )
            visual = VisionAnalysis(
                revision=document.revision,
                cut_id=cut.id,
                junction_index=0,
                left_segment_id=cut.segments[0].id,
                right_segment_id=cut.segments[1].id,
                sampling=sampling_for_mode("standard"),
                model={"model_id": "fixture"},
                frames=(),
                visible_content="画面有颜色变化。",
                possible_missing_reaction_or_action="无法确认完整动作。",
                recommendation="expand_manual_review",
                candidate_index=None,
                reason="只作证据。",
                undecidable="需预览核对。",
            )
            window = MainWindow()
            try:
                window.document = document
                window.refresh()
                window.segment_table.selectRow(0)
                window.vision_enabled.setChecked(True)
                window._analysis = base
                revision = document.revision
                window._on_visual_ready(
                    visual,
                    revision,
                    cut.id,
                    0,
                    cut.segments[0].id,
                    cut.segments[1].id,
                )
                self.assertIs(window._vision_analysis, visual)
                self.assertEqual(document.revision, revision)
                self.assertIn("画面有颜色变化", window.vision_label.text())
            finally:
                window.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
