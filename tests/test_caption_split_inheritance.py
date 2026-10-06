from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from local_slice_assistant.models import Cut, ProjectDocument, Segment, SourceInfo
from local_slice_assistant.packaging import default_packaging, make_subtitle_event, map_subtitle_events
from local_slice_assistant.project_store import save_project, load_project


class CaptionSplitInheritanceTests(unittest.TestCase):
    def document(self, start=2000000, end=6000000, speed=100):
        source = SourceInfo("fixture.mp4", 1, 8000000, 8000000, 1, 1, "fixture", False, 25, 1, 160, 90)
        first = Segment(source.relative_path, 0, 8000000, "已校准", id="first", speed_percent=speed)
        repeat = Segment(source.relative_path, 0, 8000000, "重复引用未校准", id="repeat", speed_percent=speed)
        cut = Cut("分割", [first, repeat])
        doc = ProjectDocument(tempfile.gettempdir(), "合成", {}, {source.relative_path: source}, [cut])
        pack = default_packaging()
        event = make_subtitle_event(source_file=source.relative_path, source_in_us=start,
                                   source_out_us=end, text="原文", source_kind="manual")
        pack["subtitle_events"] = [event]
        pack["subtitle_instance_overrides"][event["id"] + "@first"] = {
            "text": "已经校正", "sticker_box": dict(x=.1, y=.6, width=.7, height=.1),
            "start_offset_us": 500000, "end_offset_us": -250000,
            "sticker_start_offset_us": -750000, "new_text_start_offset_us": 250000,
            "new_text_end_offset_us": 500000,
        }
        doc.replace_packaging(cut.id, pack, "逐条校准")
        return doc

    def frames(self, cut):
        items = map_subtitle_events(cut)
        total = sum(segment.duration_us for segment in cut.segments)
        return [tuple((layer, item.text, item.sticker_box)
                      for item in items for layer in ("sticker", "new_text")
                      if getattr(item, layer + "_enabled") and
                      getattr(item, layer + "_output_in_us") <= time < getattr(item, layer + "_output_out_us"))
                for time in range(0, total, 40000)]

    def test_split_preserves_each_layer_text_box_and_repeated_source_independence(self):
        for speed in (100, 150, 200):
            with self.subTest(speed=speed):
                doc = self.document(speed=speed)
                cut = doc.active_cut
                before = self.frames(cut)
                doc.split_segment(cut.id, 0, 4000000)
                self.assertEqual(self.frames(cut), before)
                doc.split_segment(cut.id, 1, 6500000)
                self.assertEqual(self.frames(cut), before)
                with tempfile.TemporaryDirectory() as directory:
                    path = save_project(doc, Path(directory) / "split.localcut.json")
                    loaded = load_project(path, validate_sources=False).document
                    self.assertEqual(self.frames(loaded.active_cut), before)
                    loaded.undo()
                    self.assertEqual(self.frames(loaded.active_cut), before)
                    loaded.redo()
                    self.assertEqual(self.frames(loaded.active_cut), before)

    def test_offset_can_move_a_layer_completely_across_the_split(self):
        for start, end, offset in ((2000000, 3000000, 2500000), (5000000, 6000000, -2500000)):
            with self.subTest(start=start):
                doc = self.document(start, end)
                cut = doc.active_cut
                key = next(iter(cut.packaging["subtitle_instance_overrides"]))
                pack = deepcopy(cut.packaging)
                pack["subtitle_instance_overrides"][key] = dict(start_offset_us=offset, end_offset_us=offset)
                doc.replace_packaging(cut.id, pack, "整体移动")
                before = self.frames(cut)
                doc.split_segment(cut.id, 0, 4000000)
                self.assertEqual(self.frames(cut), before)

    def test_disabled_layer_remains_disabled_in_both_children(self):
        doc = self.document()
        cut = doc.active_cut
        key = next(iter(cut.packaging["subtitle_instance_overrides"]))
        cut.packaging["subtitle_instance_overrides"][key]["new_text_enabled"] = False
        before = self.frames(cut)
        doc.split_segment(cut.id, 0, 4000000)
        self.assertEqual(self.frames(cut), before)
