from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from local_slice_assistant.errors import ExportError
from local_slice_assistant.exporter import ExportSettings, export_cut
from local_slice_assistant.ffmpeg import ffmpeg_binary
from local_slice_assistant.manifest import create_project_from_video
from local_slice_assistant.packaging import make_subtitle_event, normalize_packaging
from local_slice_assistant.project_store import load_project, save_project
from tests.test_stage4_packaging import _run_ffmpeg


class CaptionSplitRenderTests(unittest.TestCase):
    def test_real_split_render_keeps_offset_cover_and_pending_cards_cannot_vanish(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "fixture.mp4"
            _run_ffmpeg(["-f", "lavfi", "-i", "color=c=0x202020:s=160x90:r=25:d=8",
                         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source)])
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            doc = create_project_from_video(source)
            cut = doc.active_cut
            pack = normalize_packaging(cut.packaging)
            event = make_subtitle_event(source_file=source.name, source_in_us=2000000,
                                        source_out_us=3000000, text="ORIGINAL", source_kind="manual")
            pack["subtitle_events"] = [event]
            pack["caption_style"]["sticker"].update(preset="pure_white", opacity=1, feather=0)
            pack["caption_style"]["new_text"]["font_size"] = 12
            pack["subtitle_instance_overrides"] = {event["id"] + "@" + cut.segments[0].id: dict(
                text="EDITED", start_offset_us=2500000, end_offset_us=2500000,
                sticker_box=dict(x=.1, y=.6, width=.7, height=.2))}
            doc.replace_packaging(cut.id, pack, "校准并移动到后段")
            settings = ExportSettings(include_packaging=True, preset="ultrafast")
            before = export_cut(doc, settings=settings).output_path
            doc.split_segment(cut.id, 0, 4000000)
            project = save_project(doc, root / "split.localcut.json")
            doc = load_project(project).document
            cut = doc.active_cut
            after = export_cut(doc, settings=settings).output_path
            def pixels(path):
                return subprocess.run([ffmpeg_binary(), "-v", "error", "-i", str(path), "-an", "-vf",
                                       "crop=2:2:20:58,scale=1:1", "-pix_fmt", "gray", "-f", "rawvideo", "-"],
                                      capture_output=True, check=True, timeout=30).stdout
            visible_before, visible_after = pixels(before), pixels(after)
            self.assertEqual(len(visible_before), 200)
            expected = list(range(113, 138))  # Half-open 4.5–5.5s on a 25fps grid.
            self.assertEqual([i for i, value in enumerate(visible_before) if value > 200], expected)
            self.assertEqual([i for i, value in enumerate(visible_after) if value > 200], expected)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
            pack = deepcopy(cut.packaging)
            pack["title_cards"] = [dict(id="pending", kind="text", anchor_segment_id=cut.segments[0].id,
                                        position="after", duration_us=400000, text="KEEP ME")]
            doc.replace_packaging(cut.id, pack, "字卡")
            doc.delete_segment(cut.id, 0)
            project = save_project(doc, root / "pending.localcut.json")
            doc = load_project(project).document
            with self.assertRaisesRegex(ExportError, "重新安排"):
                export_cut(doc, settings=settings)
            pack = deepcopy(doc.active_cut.packaging)
            pack["title_cards"][0]["enabled"] = False
            doc.replace_packaging(doc.active_cut.id, pack, "明确停用")
            rendered = export_cut(doc, settings=settings)
            self.assertLessEqual(abs(rendered.observed_duration_us - 4000000), 40000)
            self.assertEqual(doc.active_cut.packaging["title_cards"][0]["text"], "KEEP ME")
