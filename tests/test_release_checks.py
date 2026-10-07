import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from local_slice_assistant.release_checks import ReleaseCheckError, validate_ffmpeg_text, validate_portable_ffmpeg


class ReleaseFFmpegChecksTests(unittest.TestCase):
    def test_accepts_platform_specific_lgpl_builds(self):
        license_text = "GNU Lesser General Public License version 2.1 or later"
        common = "--enable-libfreetype --enable-libharfbuzz"
        validate_ffmpeg_text(license_text, common + " --enable-mediafoundation", "h264_mf", "windows")
        validate_ffmpeg_text(license_text, common + " --enable-videotoolbox", "h264_videotoolbox", "macos")

    def test_rejects_gpl_and_nonfree_even_if_h264_encoder_exists(self):
        with self.assertRaisesRegex(ReleaseCheckError, "GPL/nonfree"):
            validate_ffmpeg_text("GNU General Public License version 3", "--enable-mediafoundation",
                                 "h264_mf", "windows")
        with self.assertRaisesRegex(ReleaseCheckError, "GPL/nonfree"):
            validate_ffmpeg_text("GNU Lesser General Public License", "--enable-nonfree --enable-mediafoundation",
                                 "h264_mf", "windows")
        with self.assertRaisesRegex(ReleaseCheckError, "GPL/nonfree"):
            validate_ffmpeg_text("GNU Lesser General Public License", "--enable-libx264 --enable-mediafoundation",
                                 "h264_mf", "windows")

    def test_rejects_missing_text_filter_or_native_encoder(self):
        with self.assertRaisesRegex(ReleaseCheckError, "libharfbuzz"):
            validate_ffmpeg_text("LGPL", "--enable-libfreetype --enable-mediafoundation", "h264_mf", "windows")
        with self.assertRaisesRegex(ReleaseCheckError, "h264_videotoolbox"):
            validate_ffmpeg_text("LGPL", "--enable-libfreetype --enable-libharfbuzz --enable-videotoolbox",
                                 "h264_mf", "macos")

    def test_refuses_unknown_target(self):
        with self.assertRaisesRegex(ReleaseCheckError, "windows 或 macos"):
            validate_ffmpeg_text("LGPL", "", "", "linux")

    def test_disabled_gpl_in_banner_is_not_gpl_license(self):
        validate_ffmpeg_text(
            "configuration: --disable-gpl --disable-nonfree\nGNU Lesser General Public License",
            "--disable-gpl --disable-nonfree --enable-libfreetype --enable-libharfbuzz --enable-videotoolbox",
            "h264_videotoolbox", "macos")

    def test_real_lgpl21_wrapping_and_disabled_flags_for_both_tools(self):
        text = ("configuration: --disable-gpl --disable-nonfree\n"
                "modify it under the terms of the GNU Lesser General Public\n"
                "License as published by the Free Software Foundation; either\n"
                "version 2.1 of the License, or (at your option) any later version.")
        config = "--disable-gpl --disable-nonfree --enable-libfreetype --enable-libharfbuzz --enable-videotoolbox"
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / 'media-tool'
            binary.touch()
            with patch('local_slice_assistant.release_checks._run', side_effect=[text, config, 'h264_videotoolbox', text]):
                validate_portable_ffmpeg('macos', binary, binary)


if __name__ == "__main__":
    unittest.main()
