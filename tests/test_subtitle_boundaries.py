"""Actual boundary frames and clipped independent layers, not mid-interval samples."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from PySide6.QtGui import QImage, QColor
from local_slice_assistant.exporter import ExportSettings, export_cut
from local_slice_assistant.ffmpeg import ffmpeg_binary
from local_slice_assistant.models import Cut, Segment
from local_slice_assistant.packaging import default_packaging, make_subtitle_event, map_subtitle_events
from tests.test_stage4_packaging import _make_fixture, _imported_document


def independent_packaging(source):
    packaging = default_packaging()
    packaging["subtitle_events"] = [make_subtitle_event(
        source_file=source, source_in_us=0, source_out_us=4_000_000,
        sticker_source_in_us=0, sticker_source_out_us=1_000_000,
        new_text_source_in_us=2_000_000, new_text_source_out_us=3_000_000,
        text="TEST", source_kind="manual",
    )]
    return packaging


class SubtitleBoundaryTests(unittest.TestCase):
    def test_clipped_sticker_does_not_borrow_surviving_text_interval(self):
        cut = Cut(title="只留下新字幕", segments=[Segment("a.mp4", 2_000_000, 4_000_000, "裁剪")],
                  packaging=independent_packaging("a.mp4"))
        mapped = map_subtitle_events(cut)
        self.assertEqual(len(mapped), 1)
        self.assertFalse(mapped[0].sticker_enabled)
        self.assertTrue(mapped[0].new_text_enabled)

    def test_clipped_text_does_not_borrow_surviving_sticker_interval(self):
        cut = Cut(title="只留下遮挡", segments=[Segment("a.mp4", 0, 1_000_000, "裁剪")],
                  packaging=independent_packaging("a.mp4"))
        mapped = map_subtitle_events(cut)
        self.assertEqual(len(mapped), 1)
        self.assertTrue(mapped[0].sticker_enabled)
        self.assertFalse(mapped[0].new_text_enabled)

    def test_every_boundary_frame_of_box_image_text_and_repeated_segment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            _make_fixture(source)
            sticker = root / "white.png"
            picture = QImage(64, 24, QImage.Format.Format_RGBA8888)
            picture.fill(QColor("white"))
            self.assertTrue(picture.save(str(sticker)))
            doc = _imported_document(root, source)
            cut = doc.active_cut
            cut.segments = [Segment(source.name, 0, 2_000_000, "首次"),
                            Segment(source.name, 0, 2_000_000, "重复")]
            packaging = default_packaging()
            packaging["caption_style"]["sticker"].update(
                opacity=1.0, preset="pure_white", fit_mode="stretch", corner_radius=0,
                x=0.1, y=0.5, width=0.8, height=0.4)
            packaging["caption_style"]["new_text"].update(
                enabled=True, color="#ff0000", font_size=24, margin_y=0.02)
            packaging["subtitle_events"] = [make_subtitle_event(
                source_file=source.name, source_in_us=500_000, source_out_us=1_500_000,
                sticker_source_in_us=500_000, sticker_source_out_us=1_000_000,
                new_text_source_in_us=1_000_000, new_text_source_out_us=1_500_000,
                text="TEST", source_kind="manual")]
            for mode, shift in (("box", 0), ("image", 0), ("box", 400000), ("image", 400000)):
                with self.subTest(mode=mode, shift=shift):
                    for layer, start, end in (("sticker", 500000, 1000000), ("new_text", 1000000, 1500000)):
                        packaging["subtitle_events"][0][f"{layer}_source_in_us"] = start + shift
                        packaging["subtitle_events"][0][f"{layer}_source_out_us"] = end + shift
                    packaging["caption_style"]["sticker"]["image_path"] = str(sticker) if mode == "image" else None
                    doc.replace_packaging(cut.id, packaging, "逐帧检查")
                    cut.title = mode
                    result = export_cut(doc, settings=ExportSettings(
                        include_packaging=True, fps_num=20, fps_den=1, preset="ultrafast", crf=18))
                    raw = subprocess.run(
                        [ffmpeg_binary(), "-v", "error", "-i", str(result.output_path),
                         "-an", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                        capture_output=True, check=True, timeout=30).stdout
                    stride = 320 * 180 * 3
                    self.assertEqual(len(raw) // stride, 80)
                    cover_frames, text_frames = [], []
                    for index in range(80):
                        frame = raw[index * stride:(index + 1) * stride]
                        location = (140 * 320 + 50) * 3
                        if min(frame[location:location + 3]) > 180:
                            cover_frames.append(index)
                        red_pixels = sum(r > g + 70 and r > b + 70
                                         for r, g, b in zip(frame[0::3], frame[1::3], frame[2::3]))
                        if red_pixels > 40:
                            text_frames.append(index)
                    frames_shift = shift // 50000
                    self.assertEqual(cover_frames, [i + frames_shift for i in list(range(10, 20)) + list(range(50, 60))])
                    self.assertEqual(text_frames, [i + frames_shift for i in list(range(20, 30)) + list(range(60, 70))])
