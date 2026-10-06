from __future__ import annotations

from array import array
from copy import deepcopy
import json
import math
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from local_slice_assistant.caption_detection import detect_caption_text_presence, detect_caption_visibility
from local_slice_assistant.exporter import ExportSettings, default_export_path, export_cut
from local_slice_assistant.ffmpeg import ffmpeg_binary, probe_audio_duration, probe_media
from local_slice_assistant.manifest import import_manifest
from local_slice_assistant.models import Cut, ProjectDocument, Segment, SourceInfo
from local_slice_assistant.packaging import (
    default_packaging,
    make_subtitle_event,
    map_audio_items,
    map_subtitle_events,
    subtitle_instance_key,
)
from local_slice_assistant.transcripts import TranscriptCue
from local_slice_assistant.project_store import load_project, save_project


def _run_ffmpeg(arguments: list[str]) -> None:
    completed = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr[-2000:])


def _make_fixture(path: Path) -> None:
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=0x202020:s=320x180:r=12:d=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ]
    )


def _make_caption_visibility_fixture(path: Path) -> None:
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=0x202020:s=320x180:r=12:d=6",
            "-vf",
            (
                "drawbox=x=32:y=142:w=256:h=23:color=white:t=fill:"
                "enable='between(t,0,2)+between(t,3,6)'"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        ]
    )


def _make_text_shape_fixture(path: Path) -> None:
    font = Path(r"C:\Windows\Fonts\arial.ttf")
    if not font.exists():
        raise unittest.SkipTest("缺少系统测试字体")
    escaped_font = str(font).replace("\\", "/").replace(":", "\\:")
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=c=0x202020:s=320x180:r=12:d=6",
            "-vf",
            (
                f"drawtext=fontfile='{escaped_font}':text='HELLO 123':fontsize=26:fontcolor=yellow:"
                "x=(w-text_w)/2:y=142:enable='between(t,0,2)+between(t,3,6)',"
                "drawbox=x=12:y=15:w=30:h=30:color=white:t=fill:enable='between(t,2,3)'"
            ),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(path),
        ]
    )


def _make_audio(path: Path, frequency: int, duration: int) -> None:
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration={duration}",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )


def _audio_frequency(path: Path, seconds: float) -> tuple[float, float]:
    completed = subprocess.run(
        [
            ffmpeg_binary(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(path),
            "-t",
            "0.3",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        timeout=120,
    )
    samples = array("h")
    samples.frombytes(completed.stdout)
    if not samples:
        raise AssertionError("未能读取导出音频")
    rms = math.sqrt(sum(item * item for item in samples) / len(samples))
    crossings = sum(
        1
        for previous, current in zip(samples, samples[1:])
        if (previous < 0 <= current) or (previous >= 0 > current)
    )
    return crossings * 48_000 / (2 * len(samples)), rms


def _pixel(path: Path, seconds: float, x: int, y: int) -> tuple[int, int, int]:
    completed = subprocess.run(
        [
            ffmpeg_binary(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"crop=2:2:{x}:{y},scale=1:1:flags=neighbor,format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        timeout=120,
    )
    if len(completed.stdout) < 3:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
    return tuple(completed.stdout[:3])  # type: ignore[return-value]


def _imported_document(root: Path, source: Path) -> ProjectDocument:
    probe = probe_media(source)
    manifest = {
        "schema_version": 1,
        "example_only": False,
        "drama": "第四阶段合成夹具",
        "sources": [
            {
                "file": source.name,
                "episode": 1,
                "expected_duration_ms": probe.duration_us // 1_000,
            }
        ],
        "cuts": [
            {
                "title": "字幕包装",
                "segments": [
                    {"file": source.name, "in_ms": 0, "out_ms": 6000, "purpose": "夹具"}
                ],
            }
        ],
    }
    manifest_path = root / "剪辑清单.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return import_manifest(manifest_path, root)


class Stage4PackagingTests(unittest.TestCase):
    def test_subtitle_pair_maps_by_source_and_undo_restores_time(self) -> None:
        # 纯模型夹具避免读取用户或临时媒体：同一源重复出现时应生成两个实例。
        source = SourceInfo(
            relative_path="source.mp4",
            episode=1,
            expected_duration_us=6_000_000,
            duration_us=6_000_000,
            size=1,
            mtime_ns=1,
            quick_hash="fixture",
            has_audio=True,
            fps_num=24,
            fps_den=1,
            width=320,
            height=180,
        )
        left = Segment("source.mp4", 0, 3_000_000, "第一次")
        right = Segment("source.mp4", 0, 3_000_000, "第二次")
        cut = Cut(title="重复集", segments=[left, right])
        document = ProjectDocument(
            media_root=tempfile.gettempdir(),
            drama="包装夹具",
            original_manifest={},
            sources={source.relative_path: source},
            cuts=[cut],
        )
        packaging = default_packaging()
        event = make_subtitle_event(
            source_file="source.mp4",
            source_in_us=0,
            source_out_us=2_000_000,
            text="成组字幕",
            source_kind="srt",
        )
        packaging["subtitle_events"] = [event]
        document.replace_packaging(cut.id, packaging, "加入字幕候选")
        mapped = map_subtitle_events(cut)
        self.assertEqual(len(mapped), 2)
        self.assertEqual((mapped[0].output_in_us, mapped[0].output_out_us), (0, 2_000_000))
        self.assertEqual((mapped[1].output_in_us, mapped[1].output_out_us), (3_000_000, 5_000_000))
        adjusted = deepcopy(cut.packaging)
        adjusted["subtitle_instance_overrides"][subtitle_instance_key(event["id"], left.id)] = {
            "start_offset_us": 500_000,
            "end_offset_us": 0,
        }
        document.replace_packaging(cut.id, adjusted, "校准第一条字幕时间")
        self.assertEqual(map_subtitle_events(cut)[0].output_in_us, 500_000)
        self.assertTrue(document.undo())
        self.assertEqual(map_subtitle_events(cut)[0].output_in_us, 0)

    def test_title_card_and_speed_affect_shared_output_timeline(self) -> None:
        source = SourceInfo(
            relative_path="source.mp4",
            episode=1,
            expected_duration_us=195_000_000,
            duration_us=195_000_000,
            size=1,
            mtime_ns=1,
            quick_hash="fixture",
            has_audio=True,
            fps_num=24,
            fps_den=1,
            width=320,
            height=180,
        )
        segment = Segment("source.mp4", 0, 195_000_000, "粗剪")
        cut = Cut(title="三分十五秒", segments=[segment])
        document = ProjectDocument(
            media_root=tempfile.gettempdir(),
            drama="包装夹具",
            original_manifest={},
            sources={source.relative_path: source},
            cuts=[cut],
        )
        packaging = default_packaging()
        packaging["title_cards"] = [
            {
                "id": "card-1",
                "kind": "text",
                "anchor_segment_id": segment.id,
                "position": "after",
                "duration_us": 2_000_000,
                "text": "两秒字卡",
            }
        ]
        event = make_subtitle_event(
            source_file="source.mp4",
            source_in_us=0,
            source_out_us=2_000_000,
            text="倍速字幕",
            source_kind="manual",
        )
        packaging["subtitle_events"] = [event]
        document.replace_packaging(cut.id, packaging, "加入字卡和字幕")
        self.assertEqual(document.total_duration_us(cut.id), 197_000_000)
        document.set_segment_speed(cut.id, 0, 200)
        self.assertEqual(document.total_duration_us(cut.id), 99_500_000)
        mapped = map_subtitle_events(cut)
        self.assertEqual((mapped[0].output_in_us, mapped[0].output_out_us), (0, 1_000_000))

    def test_audio_anchor_needs_rearrangement_instead_of_drifting(self) -> None:
        source = SourceInfo(
            relative_path="source.mp4",
            episode=1,
            expected_duration_us=5_000_000,
            duration_us=5_000_000,
            size=1,
            mtime_ns=1,
            quick_hash="fixture",
            has_audio=True,
            fps_num=24,
            fps_den=1,
            width=320,
            height=180,
        )
        segment = Segment("source.mp4", 0, 5_000_000, "承载")
        cut = Cut(title="声音锚点", segments=[segment])
        document = ProjectDocument(
            media_root=tempfile.gettempdir(),
            drama="包装夹具",
            original_manifest={},
            sources={source.relative_path: source},
            cuts=[cut],
        )
        packaging = default_packaging()
        packaging["audio_items"] = [
            {
                "id": "voice-1",
                "kind": "voiceover",
                "file_path": r"D:\fixture\voice.wav",
                "anchor_segment_id": segment.id,
                "source_offset_us": 4_000_000,
                "source_in_us": 0,
                "duration_us": 500_000,
                "enabled": True,
                "mute_original": True,
                "volume": 1.0,
                "script": "夹具解说词",
            }
        ]
        document.replace_packaging(cut.id, packaging, "加入配音")
        self.assertFalse(map_audio_items(cut)[0].needs_rearrangement)
        document.adjust_segment_end(cut.id, 0, -2_000_000)
        state = map_audio_items(cut)[0]
        self.assertTrue(state.needs_rearrangement)
        self.assertIsNone(state.output_in_us)

    def test_packaged_export_only_draws_pair_during_event_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "fixture.mp4"
            _make_fixture(source)
            document = _imported_document(root, source)
            cut = document.active_cut
            packaging = default_packaging()
            packaging["subtitle_events"] = [
                make_subtitle_event(
                    source_file=source.name,
                    source_in_us=0,
                    source_out_us=2_000_000,
                    text="第一句",
                    source_kind="srt",
                ),
                make_subtitle_event(
                    source_file=source.name,
                    source_in_us=3_000_000,
                    source_out_us=6_000_000,
                    text="第二句",
                    source_kind="srt",
                ),
            ]
            document.replace_packaging(cut.id, packaging, "加入两段字幕")
            output = default_export_path(document, cut.id, packaged=True)
            export_cut(
                document,
                cut_id=cut.id,
                output_path=output,
                settings=ExportSettings(
                    preset="ultrafast", crf=30, include_packaging=True
                ),
            )
            self.assertEqual(probe_media(output).duration_us // 1_000, 6_000)
            # 新默认贴纸是下方中部的窄区（y≈123–137），避免覆盖整块下三分之一。
            visible_a = _pixel(output, 1.0, 80, 130)
            absent = _pixel(output, 2.5, 80, 130)
            visible_b = _pixel(output, 4.0, 80, 130)
            self.assertGreater(sum(visible_a), 600)
            self.assertLess(sum(absent), 180)
            self.assertGreater(sum(visible_b), 600)
            project = root / "字幕包装.localcut.json"
            save_project(document, project)
            reopened = load_project(project).document
            self.assertEqual(len(map_subtitle_events(reopened.active_cut)), 2)

    def test_transparent_png_sticker_uses_event_box_and_disappears_on_gap(self) -> None:
        """随附透明贴纸必须走真实 PNG 叠加路径，而非退回白色 drawbox。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "fixture.mp4"
            _make_fixture(source)
            sticker = Path(__file__).resolve().parents[1] / "assets" / "stickers" / "仙侠浅玉金边_透明.png"
            self.assertTrue(sticker.is_file())
            document = _imported_document(root, source)
            cut = document.active_cut
            packaging = default_packaging()
            packaging["caption_style"]["sticker"]["image_path"] = str(sticker)
            packaging["subtitle_events"] = [
                make_subtitle_event(
                    source_file=source.name, source_in_us=0, source_out_us=2_000_000,
                    text="", source_kind="screen_text_detection",
                    sticker_box={"x": 0.20, "y": 0.68, "width": 0.60, "height": 0.15},
                )
            ]
            document.replace_packaging(cut.id, packaging, "透明贴纸夹具")
            output = default_export_path(document, cut.id, packaged=True)
            export_cut(document, cut_id=cut.id, output_path=output,
                       settings=ExportSettings(preset="ultrafast", crf=30, include_packaging=True))
            covered = _pixel(output, 1.0, 160, 145)
            gap = _pixel(output, 3.0, 160, 145)
            self.assertGreater(sum(covered), sum(gap) + 80)

    def test_image_sticker_opacity_keeps_new_text_and_multiple_intervals(self) -> None:
        from PySide6.QtGui import QImage, QColor

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "fixture.mp4"
            _make_fixture(source)
            sticker = root / "white.png"
            picture = QImage(64, 24, QImage.Format.Format_RGBA8888)
            picture.fill(QColor("white"))
            self.assertTrue(picture.save(str(sticker)))
            document = _imported_document(root, source)
            cut = document.active_cut
            packaging = default_packaging()
            packaging["caption_style"]["sticker"].update(
                image_path=str(sticker), opacity=0.5, fit_mode="stretch",
                x=0.1, y=0.5, width=0.8, height=0.4,
            )
            packaging["caption_style"]["new_text"].update(
                enabled=True, color="#ff0000", font_size=24, margin_y=0.02,
            )
            packaging["subtitle_events"] = [
                make_subtitle_event(source_file=source.name, source_in_us=start,
                                    source_out_us=start + 1_000_000, text="TEST", source_kind="manual")
                for start in (0, 3_000_000)
            ]
            document.replace_packaging(cut.id, packaging, "图片贴纸与新字幕")
            output = default_export_path(document, cut.id, packaged=True)
            export_cut(document, cut_id=cut.id, output_path=output,
                       settings=ExportSettings(preset="ultrafast", crf=18, include_packaging=True))
            for second in (0.5, 3.5):
                pixel = _pixel(output, second, 50, 150)
                self.assertTrue(100 < pixel[0] < 190, pixel)
                frame = subprocess.run(
                    [ffmpeg_binary(), "-v", "error", "-ss", str(second), "-i", str(output),
                     "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                    capture_output=True, check=True, timeout=30,
                ).stdout
                red_pixels = sum(r > g + 70 and r > b + 70
                                 for r, g, b in zip(frame[0::3], frame[1::3], frame[2::3]))
                self.assertGreater(red_pixels, 40, "图片贴纸不能跳过新字幕")
            self.assertLess(max(_pixel(output, 2.0, 50, 150)), 50)

    def test_selected_caption_region_detection_only_returns_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "caption-region.mp4"
            _make_caption_visibility_fixture(source)
            document = _imported_document(root, source)
            segment = document.active_cut.segments[0]
            result = detect_caption_visibility(
                document,
                segment,
                x_ratio=0.10,
                y_ratio=0.79,
                width_ratio=0.80,
                height_ratio=0.13,
            )
            self.assertGreater(result.sample_count, 0)
            self.assertGreaterEqual(len(result.candidates), 2)
            self.assertEqual(result.candidates[0].source_in_us, 0)
            self.assertLessEqual(result.candidates[0].source_out_us, 2_250_000)
            self.assertGreaterEqual(result.candidates[-1].source_in_us, 2_750_000)
            self.assertIn("需在预览中校准", result.candidates[0].evidence)

    def test_text_shape_detection_uses_roi_and_rejects_bright_object_outside_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "text-shape.mp4"
            _make_text_shape_fixture(source)
            document = _imported_document(root, source)
            result = detect_caption_text_presence(
                document, document.active_cut.segments[0],
                x_ratio=0.10, y_ratio=0.74, width_ratio=0.80, height_ratio=0.22,
            )
            self.assertGreater(result.sample_count, 0)
            self.assertGreaterEqual(len(result.candidates), 2)
            self.assertEqual(result.candidates[0].source_in_us, 0)
            self.assertGreaterEqual(result.candidates[-1].source_in_us, 2_750_000)

    def test_sticker_and_new_text_can_have_independent_intervals(self) -> None:
        source = SourceInfo("source.mp4", 1, 4_000_000, 4_000_000, 1, 1, "fixture", True, 24, 1, 320, 180)
        segment = Segment("source.mp4", 0, 4_000_000, "独立层")
        cut = Cut(title="独立层", segments=[segment])
        document = ProjectDocument(media_root=tempfile.gettempdir(), drama="包装夹具", original_manifest={}, sources={source.relative_path: source}, cuts=[cut])
        packaging = default_packaging()
        packaging["subtitle_events"] = [make_subtitle_event(
            source_file="source.mp4", source_in_us=0, source_out_us=4_000_000,
            sticker_source_in_us=0, sticker_source_out_us=1_000_000,
            new_text_source_in_us=2_000_000, new_text_source_out_us=3_000_000,
            text="旁白", source_kind="manual",
        )]
        document.replace_packaging(cut.id, packaging, "独立字幕层")
        event = map_subtitle_events(cut)[0]
        self.assertEqual((event.sticker_output_in_us, event.sticker_output_out_us), (0, 1_000_000))
        self.assertEqual((event.new_text_output_in_us, event.new_text_output_out_us), (2_000_000, 3_000_000))

    def test_voiceover_mutes_original_only_during_its_anchor_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "fixture.mp4"
            voice = root / "voice.wav"
            _make_fixture(source)
            _make_audio(voice, 880, 1)
            document = _imported_document(root, source)
            cut = document.active_cut
            packaging = default_packaging()
            packaging["audio_items"] = [
                {
                    "id": "voiceover-1",
                    "kind": "voiceover",
                    "file_path": str(voice),
                    "anchor_segment_id": cut.segments[0].id,
                    "source_offset_us": 0,
                    "source_in_us": 0,
                    "duration_us": probe_audio_duration(voice),
                    "enabled": True,
                    "mute_original": True,
                    "volume": 1.0,
                    "script": "第一秒解说词",
                }
            ]
            document.replace_packaging(cut.id, packaging, "加入配音")
            output = default_export_path(document, cut.id, packaged=True)
            export_cut(
                document,
                cut_id=cut.id,
                output_path=output,
                settings=ExportSettings(
                    preset="ultrafast", crf=30, include_packaging=True
                ),
            )
            voice_frequency, voice_rms = _audio_frequency(output, 0.35)
            original_frequency, original_rms = _audio_frequency(output, 1.6)
            self.assertGreater(voice_rms, 100)
            self.assertGreater(original_rms, 100)
            self.assertAlmostEqual(voice_frequency, 880, delta=35)
            self.assertAlmostEqual(original_frequency, 440, delta=35)

    def test_user_image_card_is_rendered_and_counts_in_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "fixture.mp4"
            image = root / "card.png"
            _make_fixture(source)
            _run_ffmpeg(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=red:s=80x80:r=1:d=1",
                    "-frames:v",
                    "1",
                    str(image),
                ]
            )
            document = _imported_document(root, source)
            cut = document.active_cut
            packaging = default_packaging()
            packaging["title_cards"] = [
                {
                    "id": "image-card",
                    "kind": "image",
                    "anchor_segment_id": cut.segments[0].id,
                    "position": "after",
                    "duration_us": 2_000_000,
                    "text": "",
                    "image_path": str(image),
                }
            ]
            document.replace_packaging(cut.id, packaging, "加入图片卡")
            output = default_export_path(document, cut.id, packaged=True)
            export_cut(
                document,
                cut_id=cut.id,
                output_path=output,
                settings=ExportSettings(
                    preset="ultrafast", crf=30, include_packaging=True
                ),
            )
            self.assertEqual(probe_media(output).duration_us // 1_000, 8_000)
            red = _pixel(output, 6.5, 160, 90)
            self.assertGreater(red[0], 180)
            self.assertLess(red[1] + red[2], 130)

    def test_subtitle_dialog_keeps_editable_draft_until_confirmed(self) -> None:
        from PySide6.QtWidgets import QApplication

        from local_slice_assistant.packaging_dialog import PackagingDialog

        app = QApplication.instance() or QApplication([])
        source = SourceInfo(
            relative_path="source.mp4",
            episode=1,
            expected_duration_us=4_000_000,
            duration_us=4_000_000,
            size=1,
            mtime_ns=1,
            quick_hash="fixture",
            has_audio=True,
            fps_num=24,
            fps_den=1,
            width=320,
            height=180,
        )
        segment = Segment("source.mp4", 0, 4_000_000, "字幕")
        cut = Cut(title="对话框", segments=[segment])
        document = ProjectDocument(
            media_root=tempfile.gettempdir(),
            drama="包装夹具",
            original_manifest={},
            sources={source.relative_path: source},
            cuts=[cut],
        )
        dialog = PackagingDialog(
            document,
            cut.id,
            [TranscriptCue("source.mp4", 0, 1_000_000, "待校准台词")],
            segment.id,
        )
        try:
            dialog._add_from_cues()
            self.assertEqual(len(document.get_cut(cut.id).packaging), 0)
            self.assertEqual(len(dialog.result_packaging()["subtitle_events"]), 1)
            dialog._validate_and_accept()
            self.assertTrue(dialog.result())
        finally:
            dialog.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
