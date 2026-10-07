from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event, Timer
from unittest.mock import patch

from local_slice_assistant.errors import ExportCancelled, ExportError
from local_slice_assistant.exporter import ExportSettings, export_cut, default_export_path, _publish_new_export, _safe_filename
from local_slice_assistant.ffmpeg import MediaProbe, ffmpeg_binary, run_ffmpeg

from tests.helpers import make_empty_sources, patched_probe, write_manifest
from local_slice_assistant.manifest import import_manifest


class ExportContractTests(unittest.TestCase):
    def test_real_export_to_user_selected_external_directory(self):
        from tests.test_stage1_media_integration import _make_colored_media
        from local_slice_assistant.manifest import create_project_from_video
        from local_slice_assistant.paths import quick_hash
        source = self.root / 'real.mp4'
        _make_colored_media(source, color='blue', frequency=440, duration=1, fps=25, audio=True)
        document = create_project_from_video(source)
        fingerprint = quick_hash(source)
        external = Path(self.temp_dir.name) / '我的成片'
        external.mkdir()
        result = export_cut(document, output_path=external / '01.mp4', approved_output_directory=external)
        self.assertTrue(result.output_path.is_file())
        self.assertEqual(result.output_path.parent, external.resolve())
        self.assertAlmostEqual(result.observed_duration_us, 1000000, delta=50000)
        self.assertEqual(quick_hash(source), fingerprint)

    def test_explicit_output_folder_allows_external_but_preserves_boundaries_and_sources(self):
        from local_slice_assistant.exporter import _validate_output_path
        external = Path(self.temp_dir.name) / '自定义 输出'
        external.mkdir()
        target = external / '01.mp4'
        self.assertEqual(_validate_output_path(self.document, target, internal_cache=False,
                          approved_output_directory=external), target.resolve())
        with self.assertRaises(ExportError):
            _validate_output_path(self.document, external / '..' / 'escaped.mp4', internal_cache=False,
                                  approved_output_directory=external)
        source = self.root / next(iter(self.document.sources))
        before = source.read_bytes()
        with self.assertRaises(ExportError):
            _validate_output_path(self.document, source, internal_cache=False, approved_output_directory=self.root)
        self.assertEqual(source.read_bytes(), before)
        with self.assertRaises(ExportError):
            _validate_output_path(self.document, target, internal_cache=True, approved_output_directory=external)

    def test_short_names_and_repeat_suffixes_in_custom_directory(self):
        external = Path(self.temp_dir.name) / 'custom'
        external.mkdir()
        first = default_export_path(self.document, directory=external)
        self.assertEqual(first.name, '01.mp4')
        first.write_bytes(b'old')
        second = default_export_path(self.document, directory=external)
        self.assertEqual(second.name, '01_2.mp4')
        second.write_bytes(b'old2')
        self.assertEqual(default_export_path(self.document, directory=external).name, '01_3.mp4')

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "素材"
        self.root.mkdir()
        make_empty_sources(self.root)
        with patched_probe():
            self.document = import_manifest(write_manifest(self.root), self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pre_cancelled_export_never_starts_ffmpeg(self) -> None:
        cancel = Event()
        cancel.set()
        with self.assertRaises(ExportCancelled):
            export_cut(self.document, cancel_event=cancel)

    def test_low_disk_space_is_a_clear_error(self) -> None:
        with self.assertRaisesRegex(ExportError, "磁盘"):
            export_cut(
                self.document,
                settings=ExportSettings(minimum_free_bytes=2**63 - 1),
            )

    def test_export_path_cannot_leave_the_project_output_folder(self) -> None:
        with self.assertRaisesRegex(ExportError, "导出文件必须"):
            export_cut(
                self.document,
                output_path=self.root / "用户原素材.mp4",
            )

    def test_write_permission_error_is_user_facing(self) -> None:
        output_probe = MediaProbe(
            duration_us=195_000_000,
            has_audio=True,
            fps_num=24,
            fps_den=1,
            width=160,
            height=90,
        )
        with (
            patch("local_slice_assistant.exporter.run_ffmpeg"),
            patch("local_slice_assistant.exporter.probe_media", return_value=output_probe),
            patch(
                "local_slice_assistant.exporter._publish_new_export",
                side_effect=PermissionError("locked"),
            ),
            self.assertRaisesRegex(ExportError, "权限"),
        ):
            export_cut(self.document)

    def test_frozen_runtime_prefers_bundled_ffmpeg(self) -> None:
        bundle = self.root / "_internal"
        bundle.mkdir()
        bundled = bundle / "ffmpeg.exe"
        bundled.write_bytes(b"fixture")
        import local_slice_assistant.ffmpeg as ffmpeg_module

        with (
            patch.object(ffmpeg_module.shutil, "which", return_value=None),
            patch.object(ffmpeg_module.sys, "frozen", True, create=True),
            patch.object(ffmpeg_module.sys, "_MEIPASS", str(bundle), create=True),
        ):
            self.assertEqual(ffmpeg_binary(), str(bundled))

    def test_frozen_posix_runtime_uses_extensionless_bundled_ffmpeg(self) -> None:
        bundle = self.root / "_internal"
        bundle.mkdir()
        bundled = bundle / "ffmpeg"
        bundled.write_bytes(b"fixture")
        import local_slice_assistant.ffmpeg as ffmpeg_module
        from types import SimpleNamespace

        with (
            patch.object(ffmpeg_module, "os", SimpleNamespace(
                name="posix", environ=ffmpeg_module.os.environ, fspath=ffmpeg_module.os.fspath
            )),
            patch.object(ffmpeg_module.shutil, "which", return_value=None),
            patch.object(ffmpeg_module.sys, "frozen", True, create=True),
            patch.object(ffmpeg_module.sys, "_MEIPASS", str(bundle), create=True),
        ):
            self.assertEqual(ffmpeg_binary(), str(bundled))

    def test_default_export_is_numbered_and_explicit_existing_file_never_encodes(self):
        output = default_export_path(self.document)
        expected_parent = self.root.parent / "映序项目" / self.root.name / "默认导出"
        self.assertEqual(output.parent, expected_parent.resolve())
        self.assertFalse(output.is_relative_to(self.root.resolve()))
        output.write_bytes(b"previous export")
        second = default_export_path(self.document)
        self.assertNotEqual(second, output)
        self.assertEqual(second.stem, output.stem + "_2")
        with patch("local_slice_assistant.exporter.run_ffmpeg") as encode, self.assertRaisesRegex(ExportError, "不会覆盖"):
            export_cut(self.document, output_path=output)
        encode.assert_not_called()
        self.assertEqual(output.read_bytes(), b"previous export")

    def test_sibling_exports_are_not_discovered_as_source_videos(self):
        from local_slice_assistant.exporter import export_directory
        from local_slice_assistant.paths import discover_media_files

        source = self.root / "01.mp4"
        source.write_bytes(b"source")
        sibling_output = export_directory(self.root)
        (sibling_output / "01.mp4").write_bytes(b"finished")
        self.assertNotIn((sibling_output / "01.mp4").resolve(), discover_media_files(self.root))

    def test_export_names_preserve_episode_numbers_and_letters(self):
        for title in ("第01集 ABC_xyz_剪辑", "S01E03", "片段3-5"):
            self.assertEqual(_safe_filename(title), title)
        self.assertEqual(_safe_filename("a/b:c\\d\x00e"), "a_b_c_d_e")
        self.assertEqual(_safe_filename("CON.note"), "_CON.note")
        self.assertEqual(_safe_filename(".."), "未命名切片")

    def test_publish_does_not_overwrite_destination_created_during_encode(self):
        temporary = self.root / "partial.mp4"
        target = self.root / "race.mp4"
        temporary.write_bytes(b"new")
        target.write_bytes(b"other writer")
        with self.assertRaises(FileExistsError):
            _publish_new_export(temporary, target)
        self.assertEqual(target.read_bytes(), b"other writer")
        self.assertEqual(temporary.read_bytes(), b"new")

    def test_export_applies_decoder_and_processing_budgets(self):
        output_probe = MediaProbe(195000000, True, 24, 1, 160, 90)
        for mode, count in (("standard", 6), ("saver", 2)):
            with self.subTest(mode=mode):
                self.document.resource_settings["mode"] = mode
                with patch("local_slice_assistant.exporter.run_ffmpeg") as encode, patch("local_slice_assistant.exporter.probe_media", return_value=output_probe), patch("local_slice_assistant.exporter._publish_new_export"):
                    export_cut(self.document)
                arguments = encode.call_args.args[0]
                self.assertEqual(arguments[arguments.index("-threads:v") + 1], str(count))
                self.assertEqual(arguments[arguments.index("-filter_complex_threads") + 1], str(count))
                for index, value in enumerate(arguments):
                    if value == "-i":
                        self.assertEqual(arguments[index - 2:index], ["-threads", "1"])

    def test_resource_failure_cleans_only_this_export_and_can_retry(self):
        output = default_export_path(self.document)
        prior = output.with_name('prior.mp4')
        prior.write_bytes(b'keep previous export')
        def fail(arguments, **kwargs):
            Path(arguments[-1]).write_bytes(b'partial')
            raise ExportError('内存保护阈值')
        with patch('local_slice_assistant.exporter.run_ffmpeg', side_effect=fail), self.assertRaisesRegex(ExportError, '保护阈值'):
            export_cut(self.document, output_path=output)
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.glob('*.partial.mp4')), [])
        self.assertEqual(prior.read_bytes(), b'keep previous export')
        def succeed(arguments, **kwargs):
            Path(arguments[-1]).write_bytes(b'complete')
        with patch('local_slice_assistant.exporter.run_ffmpeg', side_effect=succeed), patch('local_slice_assistant.exporter.probe_media', return_value=MediaProbe(195000000, True, 24, 1, 160, 90)):
            export_cut(self.document, output_path=output)
        self.assertEqual(output.read_bytes(), b'complete')
        self.assertEqual(prior.read_bytes(), b'keep previous export')

    def test_running_ffmpeg_can_be_cancelled(self) -> None:
        cancel = Event()
        timer = Timer(0.35, cancel.set)
        timer.start()
        try:
            with self.assertRaisesRegex(ExportCancelled, "取消"):
                run_ffmpeg(
                    [
                        "-re",
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc=size=16x16:rate=10",
                        "-t",
                        "10",
                        "-f",
                        "null",
                        "-",
                    ],
                    cancel_event=cancel,
                )
        finally:
            timer.cancel()
