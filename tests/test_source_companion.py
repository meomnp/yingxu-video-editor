import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.build_source_companion import (
    QT_PROVISIONING_FILES,
    QT_SUPER_COMMIT,
    SourceArchive,
    SourceBundleError,
    _read_qt_provisioning_sources,
    build_bundle,
)


class SourceCompanionTests(unittest.TestCase):
    def test_qt_provisioning_inputs_are_pinned_to_expected_commit_and_blobs(self):
        outputs = [QT_SUPER_COMMIT + "\n"]
        for path, blob, _ in QT_PROVISIONING_FILES:
            outputs.extend((blob + "\n", path.encode("utf-8")))
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            (cache / "qt5-super-v6.11.2").mkdir()
            with patch("scripts.build_source_companion.subprocess.run") as run:
                run.side_effect = [
                    type("Result", (), {"stdout": output})() for output in outputs
                ]
                # Git rev-parse calls request text, while git show returns bytes.
                loaded = _read_qt_provisioning_sources(cache)
        self.assertEqual(loaded, [(member, path.encode("utf-8"))
                                  for path, _, member in QT_PROVISIONING_FILES])
        self.assertEqual(run.call_count, 1 + 2 * len(QT_PROVISIONING_FILES))

    def _repository(self, root: Path) -> Path:
        root.mkdir(parents=True)
        (root / "docs").mkdir()
        (root / "scripts").mkdir()
        for relative in (
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "docs/第三方对应源码清单.md",
            "scripts/build_ffmpeg_lgpl_validation.sh",
        ):
            (root / relative).write_text(relative, encoding="utf-8")
        return root

    def test_builds_separate_archive_after_sha256_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "sources"
            source_root.mkdir()
            source = source_root / "fixture.tar.xz"
            source.write_bytes(b"fixture source")
            entry = SourceArchive(
                source.name, hashlib.sha256(source.read_bytes()).hexdigest(), "fixture component"
            )
            output = build_bundle(source_root, self._repository(root / "repo"), root / "out.zip",
                                  archives=(entry,))
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read("sources/fixture.tar.xz"), b"fixture source")
                readme = archive.read("README.md").decode("utf-8")
                self.assertIn("不包含映序应用代码", readme)
                self.assertIn(entry.sha256, readme)
                self.assertIn("LICENSE", archive.namelist())

    def test_hash_mismatch_and_existing_output_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "sources"
            source_root.mkdir()
            source = source_root / "fixture.tar.xz"
            source.write_bytes(b"fixture")
            entry = SourceArchive(source.name, "0" * 64, "fixture")
            repo = self._repository(root / "repo")
            output = root / "out.zip"
            with self.assertRaisesRegex(SourceBundleError, "校验失败"):
                build_bundle(source_root, repo, output, archives=(entry,))
            self.assertFalse(output.exists())

            output.write_bytes(b"keep")
            with self.assertRaisesRegex(SourceBundleError, "拒绝覆盖"):
                build_bundle(source_root, repo, output, archives=(entry,))
            self.assertEqual(output.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
