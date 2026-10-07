import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import fetch_runtime_licenses as runtime_licenses


class RuntimeLicenseTests(unittest.TestCase):
    def test_pinned_license_files_are_verified_before_writing(self):
        bodies = {"one.txt": b"license one", "two.txt": b"license two"}
        entries = tuple(
            (name, f"https://example.invalid/{name}", hashlib.sha256(body).hexdigest())
            for name, body in bodies.items()
        )

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        def open_url(request, timeout):
            self.assertEqual(timeout, 30)
            return Response(bodies[Path(request.full_url).name])

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(runtime_licenses, "LICENSES", entries), \
                patch.object(runtime_licenses, "urlopen", side_effect=open_url):
            paths = runtime_licenses.fetch_runtime_licenses(
                directory, python_version="3.13.16", openssl_version="OpenSSL 3.5.9 test"
            )
            self.assertEqual([path.name for path in paths], ["one.txt", "two.txt"])
            self.assertEqual([path.read_bytes() for path in paths], list(bodies.values()))
            with self.assertRaises(FileExistsError):
                runtime_licenses.fetch_runtime_licenses(
                    directory, python_version="3.13.16", openssl_version="OpenSSL 3.5.9 test"
                )

    def test_version_mismatch_is_rejected_before_network_or_filesystem_changes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime_licenses, "urlopen") as open_url:
            with self.assertRaisesRegex(ValueError, "3.13.16"):
                runtime_licenses.fetch_runtime_licenses(
                    directory, python_version="3.13.15", openssl_version="OpenSSL 3.5.9 test"
                )
            open_url.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_hash_mismatch_leaves_no_license_file(self):
        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        entries = (("one.txt", "https://example.invalid/one.txt", "0" * 64),)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(runtime_licenses, "LICENSES", entries), \
                patch.object(runtime_licenses, "urlopen", return_value=Response(b"unexpected")):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                runtime_licenses.fetch_runtime_licenses(
                    directory, python_version="3.13.16", openssl_version="OpenSSL 3.5.9 test"
                )
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
