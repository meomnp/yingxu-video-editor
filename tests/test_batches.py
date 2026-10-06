import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from local_slice_assistant.batches import create_batch, ensure_batch, versioned_path


class BatchTests(unittest.TestCase):
    def test_batches_never_reuse_deleted_numbers_and_keep_legacy(self):
        with tempfile.TemporaryDirectory() as root:
            doc = SimpleNamespace(media_root=root, drama='示例短剧', planning_context={})
            legacy = Path(root) / 'old.json'
            legacy.write_text('old')
            one = create_batch(doc)
            self.assertEqual(ensure_batch(doc), one)
            two = create_batch(doc)
            self.assertTrue(two.name.endswith('第002批'))
            for child in two.iterdir():
                child.rmdir()
            two.rmdir()
            three = create_batch(doc)
            self.assertTrue(three.name.endswith('第003批'))
            self.assertEqual(legacy.read_text(), 'old')
            self.assertEqual(Path(doc.planning_context['export_directory']), three / '成片')
            item = three / 'AI材料' / '方案.json'
            item.write_text('old')
            self.assertEqual(versioned_path(item).name, '方案_v02.json')
            self.assertEqual(item.read_text(), 'old')
