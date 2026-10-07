import tempfile
import unittest
from pathlib import Path

from local_slice_assistant.api_artifacts import api_artifact_directory, api_saved_file_path


class ApiArtifactDirectoryTests(unittest.TestCase):
    def test_api_artifacts_are_in_sibling_project_not_media_folder(self):
        with tempfile.TemporaryDirectory() as parent:
            media = Path(parent) / "剧集甲"
            media.mkdir()
            directory = api_artifact_directory(media, create=True)
            self.assertEqual(directory, Path(parent) / "映序项目" / "剧集甲" / "AI材料")
            self.assertFalse(directory.is_relative_to(media))
            self.assertTrue(directory.is_dir())

    def test_batch_api_response_is_saved_under_that_batch(self):
        with tempfile.TemporaryDirectory() as parent:
            media = Path(parent) / "剧集甲"
            media.mkdir()
            batch = Path(parent) / "映序项目" / "剧集甲" / "合成示例_第002批"
            batch.mkdir(parents=True)
            directory = api_artifact_directory(media, create=True, batch_directory=batch)
            self.assertEqual(directory, batch / "AI材料")
            self.assertTrue(directory.is_dir())

    def test_history_saved_path_can_be_resolved_and_opened(self):
        with tempfile.TemporaryDirectory() as parent:
            media = Path(parent) / "剧集甲"
            media.mkdir()
            artifacts = api_artifact_directory(media, create=True)
            response = artifacts / "回复.md"
            response.write_text("回复", encoding="utf-8")
            resolved = api_saved_file_path(media, {"saved_path": str(response)})
            self.assertEqual(resolved, response)
            self.assertTrue(resolved.is_file())

    def test_history_rejects_saved_path_outside_project(self):
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as outside:
            media = Path(parent) / "剧集甲"
            media.mkdir()
            escaped = Path(outside) / "secret.md"
            self.assertIsNone(api_saved_file_path(media, {"saved_path": str(escaped)}))

    def test_existing_symlink_cannot_redirect_api_artifacts(self):
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as outside:
            base = Path(parent)
            media = base / "剧集甲"
            media.mkdir()
            try:
                (base / "映序项目").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前 Windows 环境不允许创建目录符号链接")
            with self.assertRaisesRegex(ValueError, "上级目录"):
                api_artifact_directory(media, create=True)


if __name__ == "__main__":
    unittest.main()
