import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication
from local_slice_assistant.sticker_library import StickerLibraryDialog


class StickerLibraryTests(unittest.TestCase):
    def test_select_deduplicate_and_apply_user_image(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "自有贴纸.png"
            image = QImage(80, 20, QImage.Format.Format_RGBA8888)
            image.fill(QColor(255, 255, 255, 128))
            self.assertTrue(image.save(str(path)))
            dialog = StickerLibraryDialog(str(path))
            count = dialog.items.count()
            dialog.add_paths([path, path])
            self.assertEqual(dialog.items.count(), count)
            dialog.apply()
            self.assertEqual(dialog.selected_path, str(path.resolve()))
            self.assertEqual(dialog.result(), 1)
            dialog.close()
            app.processEvents()
