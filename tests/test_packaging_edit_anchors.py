from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from local_slice_assistant.errors import ManifestValidationError
from local_slice_assistant.models import Cut, ProjectDocument, Segment, SourceInfo
from local_slice_assistant.packaging import default_packaging, make_subtitle_event, map_audio_items, reassign_attachment, title_cards_for_cut
from local_slice_assistant.project_store import save_project, load_project


class PackagingEditAnchorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio_file = self.root / "reference.wav"
        self.audio_file.write_bytes(b"test fixture; never rendered")
        self.first = Segment("source.mp4", 1000000, 10000000, "第一次使用", id="a")
        self.repeat = Segment("source.mp4", 1000000, 10000000, "重复使用", id="b")
        self.cut = Cut("声音定位", [self.first, self.repeat])
        source = SourceInfo("source.mp4", 1, 20000000, 20000000, 1, 1, "fixture", True, 25, 1, 160, 90)
        self.doc = ProjectDocument(str(self.root), "合成样例", {}, {source.relative_path: source}, [self.cut])
        pack = default_packaging()
        pack["audio_items"] = [self.audio("first", "a", 3000000), self.audio("repeat", "b", 1000000)]
        self.doc.replace_packaging(self.cut.id, pack, "音频导入")

    def audio(self, identity, segment, offset):
        return dict(id=identity, kind="voiceover", file_path=str(self.audio_file),
                    anchor_segment_id=segment, source_offset_us=offset, source_in_us=0,
                    duration_us=500000, enabled=True, mute_original=True, volume=1.0, script="合成台词")

    def positions(self):
        return {item.item_id: item.output_in_us for item in map_audio_items(self.cut)}

    def roundtrip(self):
        save_project(self.doc, self.root / "check.localcut.json")
        self.doc = load_project(self.root / "check.localcut.json", validate_sources=False).document
        self.cut = self.doc.active_cut

    def test_trim_in_preserves_absolute_source_point_and_undo_redo(self):
        self.assertEqual(self.positions(), {"first": 3000000, "repeat": 10000000})
        self.doc.adjust_segment_start(self.cut.id, 0, 1000000)
        self.assertEqual(self.positions(), {"first": 2000000, "repeat": 9000000})
        self.roundtrip()
        self.assertEqual(self.positions()["first"], 2000000)
        self.doc.undo()
        self.assertEqual(self.positions()["first"], 3000000)
        self.doc.redo()
        self.assertEqual(self.positions()["first"], 2000000)

    def test_legacy_relative_anchor_is_frozen_before_first_trim(self):
        for item in self.cut.packaging["audio_items"]:
            item.pop("anchor_source_us")
        self.roundtrip()
        self.doc.adjust_segment_start(self.cut.id, 0, 1000000)
        self.assertEqual(self.positions()["first"], 2000000)
        self.doc.undo()
        self.assertEqual(self.positions()["first"], 3000000)

    def test_trim_away_anchor_marks_rearrangement_never_moves_it_to_new_dialogue(self):
        self.doc.adjust_segment_start(self.cut.id, 0, 3500000)
        mapped = map_audio_items(self.cut)[0]
        self.assertTrue(mapped.needs_rearrangement)
        self.assertIsNone(mapped.output_in_us)
        self.roundtrip()  # An unresolved anchor can still be saved safely.
        self.doc.undo()
        self.assertEqual(self.positions()["first"], 3000000)

    def test_reorder_and_speed_keep_repeated_source_instances_independent(self):
        self.doc.adjust_segment_start(self.cut.id, 0, 1000000)
        self.doc.reorder_segments(self.cut.id, ["b", "a"])
        self.doc.set_segment_speed(self.cut.id, 1, 200)
        self.assertEqual(self.positions(), {"first": 10000000, "repeat": 1000000})

    def test_split_moves_right_side_voice_and_after_card_without_changing_output_time(self):
        pack = deepcopy(self.cut.packaging)
        pack["title_cards"] = [dict(id="card", kind="text", anchor_segment_id="a", position="after",
                                    duration_us=1000000, text="后置卡片")]
        self.doc.replace_packaging(self.cut.id, pack, "字卡")
        before = self.positions()
        self.doc.split_segment(self.cut.id, 0, 3000000)
        child_id = self.cut.segments[1].id
        self.assertEqual(self.cut.packaging["audio_items"][0]["anchor_segment_id"], child_id)
        self.assertEqual(self.cut.packaging["title_cards"][0]["anchor_segment_id"], child_id)
        self.assertEqual(self.positions(), before)
        self.roundtrip()
        self.doc.undo()
        self.assertEqual(self.cut.packaging["audio_items"][0]["anchor_segment_id"], "a")
        self.assertEqual(self.positions(), before)
        self.doc.redo()
        self.assertEqual(self.positions(), before)

    def test_voice_crossing_new_split_is_flagged_not_truncated(self):
        self.doc.split_segment(self.cut.id, 0, 4250000)
        mapped = map_audio_items(self.cut)[0]
        self.assertTrue(mapped.needs_rearrangement)
        self.assertEqual(self.cut.packaging["audio_items"][0]["duration_us"], 500000)
        self.roundtrip()
        self.doc.undo()
        self.assertEqual(self.positions()["first"], 3000000)

    def test_delete_carrier_keeps_save_valid_and_undo_restores_all_attached_layers(self):
        pack = deepcopy(self.cut.packaging)
        event = make_subtitle_event(source_file="source.mp4", source_in_us=1000000,
                                    source_out_us=2000000, text="合成字幕", source_kind="manual")
        pack["subtitle_events"] = [event]
        key = event["id"] + "@a"
        pack["subtitle_instance_overrides"] = {key: {"text": "校正字幕", "sticker_enabled": False}}
        pack["title_cards"] = [dict(id="card", kind="text", anchor_segment_id="a", position="after",
                                    duration_us=1000000, text="本段卡片"),
                               dict(id="end", kind="text", anchor_segment_id="a", position="end",
                                    duration_us=1000000, text="全片结尾")]
        self.doc.replace_packaging(self.cut.id, pack, "绑定包装")
        self.doc.delete_segment(self.cut.id, 0)
        self.assertEqual([item["id"] for item in self.cut.packaging["audio_items"]], ["first", "repeat"])
        self.assertEqual([item["id"] for item in self.cut.packaging["title_cards"]], ["card", "end"])
        self.assertTrue(map_audio_items(self.cut)[0].needs_rearrangement)
        self.assertEqual(self.cut.packaging["audio_items"][0]["detached_segment"]["id"], "a")
        self.assertEqual(self.cut.packaging["title_cards"][0]["detached_segment"]["source_file"], "source.mp4")
        self.assertIsNone(self.cut.packaging["title_cards"][1]["anchor_segment_id"])
        self.assertEqual(self.cut.packaging["subtitle_instance_overrides"], {})
        self.assertEqual(len(self.cut.packaging["subtitle_events"]), 1)  # Shared by the repeated source.
        self.roundtrip()
        self.doc.undo()
        self.doc.validate()
        self.assertIn(key, self.cut.packaging["subtitle_instance_overrides"])
        self.assertEqual(len(self.cut.packaging["audio_items"]), 2)
        self.doc.redo()
        self.doc.validate()
        self.assertEqual(self.audio_file.read_bytes(), b"test fixture; never rendered")

    def test_pending_voice_can_be_saved_and_explicitly_reassigned_without_changing_file(self):
        self.doc.delete_segment(self.cut.id, 0)
        self.roundtrip()
        self.assertIsNone(self.positions()["first"])
        self.doc.replace_packaging(self.cut.id, reassign_attachment(self.cut, "audio_items", "first", "b", source_offset_us=2500000), "重新安排")
        self.assertEqual(self.positions()["first"], 2500000)
        self.assertNotIn("detached_segment", self.cut.packaging["audio_items"][0])
        self.roundtrip()
        self.doc.undo()
        self.assertTrue(map_audio_items(self.cut)[0].needs_rearrangement)
        self.doc.redo()
        self.assertEqual(self.positions()["first"], 2500000)
        self.assertEqual(self.audio_file.read_bytes(), b"test fixture; never rendered")

    def test_reassignment_does_not_truncate_or_mutate_on_failure(self):
        self.doc.delete_segment(self.cut.id, 0)
        before = deepcopy(self.doc.to_dict())
        with self.assertRaisesRegex(ManifestValidationError, "未截断"):
            reassign_attachment(self.cut, "audio_items", "first", "b", source_offset_us=8800000)
        self.assertEqual(self.doc.to_dict(), before)
        pack = deepcopy(self.cut.packaging)
        pack["audio_items"][0].pop("detached_segment")
        with self.assertRaises(ManifestValidationError):
            self.doc.replace_packaging(self.cut.id, pack, "损坏的待安排记录")
        self.assertEqual(self.doc.to_dict(), before)

    def test_disabled_card_is_retained_but_not_rendered_and_can_be_reassigned(self):
        pack = deepcopy(self.cut.packaging)
        pack["title_cards"] = [dict(id="card", kind="text", anchor_segment_id="a", position="after", duration_us=1000000, text="待安排")]
        self.doc.replace_packaging(self.cut.id, pack, "字卡")
        self.doc.delete_segment(self.cut.id, 0)
        pack = deepcopy(self.cut.packaging)
        pack["title_cards"][0]["enabled"] = False
        self.doc.replace_packaging(self.cut.id, pack, "停用")
        self.assertEqual(title_cards_for_cut(self.cut), [])
        self.doc.replace_packaging(self.cut.id, reassign_attachment(self.cut, "title_cards", "card", "b"), "安排")
        self.roundtrip()
        card = self.cut.packaging["title_cards"][0]
        self.assertNotIn("detached_segment", card)
        self.assertFalse(card["enabled"])  # Reassign must not silently enable.
        self.assertEqual(card["anchor_segment_id"], "b")

    def test_invalid_edit_is_atomic_including_packaging_and_history(self):
        before = deepcopy(self.doc.to_dict())
        with self.assertRaises(ManifestValidationError):
            self.doc.adjust_segment_end(self.cut.id, 0, 30000000)
        self.assertEqual(self.doc.to_dict(), before)
        pack = deepcopy(self.cut.packaging)
        pack["audio_items"][0]["anchor_source_us"] = True
        with self.assertRaises(ManifestValidationError):
            self.doc.replace_packaging(self.cut.id, pack, "无效锚点")
        self.assertEqual(self.doc.to_dict(), before)
