import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.audit_portable_archive import scan_archive


class PortableArchiveAuditTests(unittest.TestCase):
    def test_scans_utf8_utf16le_and_archive_names_without_echoing_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "candidate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("映序/path.txt", b"C:\\Users\\private\\clip.mp4")
                archive.writestr("映序/config.dat", "sk-abcdefghijklmnopqrstuvwxyz123456".encode())
                archive.writestr("映序/runtime.bin", "C:\\Users\\private\\secret.txt".encode("utf-16le"))

            findings = scan_archive(archive_path)

            self.assertIn(("映序/path.txt", "Windows user profile path"), findings)
            self.assertIn(("映序/config.dat", "DeepSeek-style API key"), findings)
            self.assertIn(("映序/runtime.bin", "Windows user profile path"), findings)
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", repr(findings))

    def test_private_strings_are_runtime_only_and_allow_text_is_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "candidate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("映序/BUILD_INFO.txt", "/Users/buildbot/work/build")
                archive.writestr("映序/data.txt", "custom-private-marker")

            findings = scan_archive(archive_path, forbidden=["custom-private-marker"],
                                    allowed=["/Users/buildbot"])

            self.assertNotIn(("映序/BUILD_INFO.txt", "macOS/Linux user home path"), findings)
            self.assertIn(("映序/data.txt", "user-supplied private string #1"), findings)

    def test_allow_text_only_suppresses_matching_occurrence_not_same_file(self):
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "candidate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "映序/metadata.bin",
                    "C:\\Users\\buildbot\\work\\ C:\\Users\\private\\secret\\ "
                    "custom-private-marker",
                )

            findings = scan_archive(
                archive_path,
                forbidden=["custom-private-marker"],
                allowed=[r"C:\Users\buildbot"],
            )

            self.assertIn(("映序/metadata.bin", "Windows user profile path"), findings)
            self.assertIn(("映序/metadata.bin", "user-supplied private string #1"), findings)


if __name__ == "__main__":
    unittest.main()
