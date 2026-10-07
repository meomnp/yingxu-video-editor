import os
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from local_slice_assistant.gui import MainWindow
from local_slice_assistant.packaging_dialog import PackagingDialog
from local_slice_assistant.packaging import map_audio_items
from tests import test_caption_split_inheritance as caption_fixtures


class PackagingRecoveryGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.doc = caption_fixtures.CaptionSplitInheritanceTests().document()
        # The shared caption fixture uses tempfile.gettempdir() itself as its
        # media root. On hosts where AppData\Local is redirected, its parent
        # is outside the resolved media tree and export_directory correctly
        # refuses to write there. Give GUI tests an isolated D-drive media root.
        build_root = Path(__file__).resolve().parents[1] / "build"
        self._temporary_media = tempfile.TemporaryDirectory(dir=build_root)
        self.doc.media_root = self._temporary_media.name
        self.cut = self.doc.active_cut
        self.window = MainWindow()
        self.window.document = self.doc
        self.window.refresh()

    def tearDown(self):
        self.window._set_project_baseline(saved=True)
        self.window.close()
        self.app.processEvents()
        self._temporary_media.cleanup()

    def test_edit_text_preserves_detected_box_and_split_timing_without_double_offset(self):
        self.doc.split_segment(self.cut.id, 0, 4000000)
        dialog = PackagingDialog(self.doc, self.cut.id, [], None)
        event = dialog._selected_event()
        before = deepcopy(dialog.draft["subtitle_instance_overrides"][event.instance_key])
        dialog.event_text.setText("新文案")
        dialog._apply_selected()
        after = dialog.draft["subtitle_instance_overrides"][event.instance_key]
        self.assertEqual(after["timing_source_bounds"], before["timing_source_bounds"])
        self.assertEqual(after["sticker_box"], before["sticker_box"])
        self.assertEqual(after["sticker_start_offset_us"], -250000)
        self.assertNotIn("start_offset_us", after)
        self.assertEqual(after["text"], "新文案")
        dialog.close()

    def pending(self):
        pack = deepcopy(self.cut.packaging)
        pack["audio_items"] = [dict(id="voice", kind="voiceover", file_path="fixture.wav", anchor_segment_id="first",
                                    source_offset_us=0, source_in_us=0, duration_us=500000, enabled=True, mute_original=True, script="保留词")]
        self.doc.replace_packaging(self.cut.id, pack, "导入")
        self.doc.delete_segment(self.cut.id, 0)
        self.window.refresh()
        self.window.segment_table.selectRow(0)

    def test_pending_item_shown_first_and_reassign_is_undoable(self):
        self.pending()
        calls = []
        def choose(*args):
            calls.append(args)
            return args[3][0], True
        with patch("local_slice_assistant.gui.QInputDialog.getItem", side_effect=choose), patch("local_slice_assistant.gui.QInputDialog.getInt", return_value=(1250, True)):
            self.window.toggle_selected_audio_item()
        self.assertIn("待重新安排", calls[0][3][0])
        self.assertEqual(map_audio_items(self.cut)[0].output_in_us, 1250000)
        self.doc.undo()
        self.assertTrue(map_audio_items(self.cut)[0].needs_rearrangement)

    def test_cancel_or_disable_does_not_discard_pending_work(self):
        self.pending()
        before = self.doc.to_dict()
        with patch("local_slice_assistant.gui.QInputDialog.getItem", side_effect=lambda *args: (args[3][0], True)), patch("local_slice_assistant.gui.QInputDialog.getInt", return_value=(0, False)):
            self.window.toggle_selected_audio_item()
        self.assertEqual(self.doc.to_dict(), before)
        decisions = iter((0, 1))
        with patch("local_slice_assistant.gui.QInputDialog.getItem", side_effect=lambda *args: (args[3][next(decisions)], True)):
            self.window.toggle_selected_audio_item()
        self.assertFalse(self.cut.packaging["audio_items"][0]["enabled"])
        self.assertTrue(map_audio_items(self.cut)[0].needs_rearrangement)
        self.assertEqual(self.cut.packaging["audio_items"][0]["script"], "保留词")
