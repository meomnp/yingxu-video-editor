import unittest

from local_slice_assistant.release_checks import ReleaseCheckError, validate_ffmpeg_text


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


if __name__ == "__main__":
    unittest.main()
