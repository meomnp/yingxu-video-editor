import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from local_slice_assistant.batches import create_batch, ensure_batch, versioned_path
from local_slice_assistant.paths import discover_media_files, resolve_excluded_dirs
from local_slice_assistant.project_store import default_project_path


class BatchTests(unittest.TestCase):
    def test_batches_never_reuse_deleted_numbers_and_keep_legacy(self):
        with tempfile.TemporaryDirectory() as root:
            doc = SimpleNamespace(media_root=root, drama='示例短剧', planning_context={})
            legacy = Path(root) / 'old.json'
            legacy.write_text('old')
            one = create_batch(doc)
            self.assertEqual(one.parent.parent, Path(root).resolve().parent / '映序项目')
            self.assertFalse(one.is_relative_to(Path(root).resolve()))
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

    def test_batches_for_same_level_source_folders_are_separate(self):
        with tempfile.TemporaryDirectory() as parent:
            roots = [Path(parent) / name for name in ('剧集甲', '剧集乙')]
            for root in roots:
                root.mkdir()
            docs = [SimpleNamespace(media_root=str(root), drama=root.name, planning_context={}) for root in roots]
            first, second = [create_batch(doc) for doc in docs]
            self.assertNotEqual(first.parent, second.parent)
            self.assertEqual(first.parent.parent, second.parent.parent)

    def test_default_editable_project_is_outside_selected_media_folder(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / '剧集甲'
            root.mkdir()
            path = default_project_path(root, '剧集甲')
            self.assertEqual(path, Path(parent) / '映序项目' / '剧集甲' / '工程' / '剧集甲.localcut.json')
            self.assertFalse(path.is_relative_to(root))

    def test_legacy_nested_project_videos_are_excluded_from_folder_import_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / '01.mp4'
            old_export = root / '映序项目' / '剧名_第001批' / '成片' / '01.mp4'
            source.write_bytes(b'source')
            old_export.parent.mkdir(parents=True)
            old_export.write_bytes(b'previous export')
            found = discover_media_files(root, exclusions=resolve_excluded_dirs(root))
            self.assertEqual(found, [source.resolve()])
