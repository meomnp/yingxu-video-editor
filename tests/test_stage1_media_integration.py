from __future__ import annotations

from array import array
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_slice_assistant.exporter import export_cut
from local_slice_assistant.ffmpeg import ffmpeg_binary, ffprobe_binary, probe_media
from local_slice_assistant.manifest import import_manifest
from local_slice_assistant.paths import (
    discover_media_files,
    quick_hash,
    resolve_excluded_dirs,
)
from local_slice_assistant.preview import create_junction_preview
from local_slice_assistant.project_store import load_project, save_project


def _run_ffmpeg(arguments: list[str]) -> None:
    completed = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr[-2000:])


def _make_colored_media(
    path: Path,
    *,
    duration: int,
    color: str,
    fps: int,
    audio: bool,
    frequency: int = 880,
    change_to: str | None = None,
    vfr: bool = False,
) -> None:
    arguments = [
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=160x90:r={fps}:d={duration}",
    ]
    if audio:
        arguments += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration={duration}",
        ]
    if change_to:
        arguments += [
            "-filter:v",
            (
                "drawbox=x=0:y=0:w=iw:h=ih:"
                f"color={change_to}:t=fill:enable='gte(t,{duration // 2})'"
            ),
        ]
    if vfr:
        arguments += ["-filter:v", "select='not(mod(n,5))'", "-fps_mode", "vfr"]
    arguments += [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "30",
        "-pix_fmt",
        "yuv420p",
    ]
    if audio:
        arguments += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "48k"]
    else:
        arguments += ["-map", "0:v:0", "-an"]
    arguments.append(str(path))
    _run_ffmpeg(arguments)


def _pixel_at(path: Path, seconds: float) -> tuple[int, int, int]:
    completed = subprocess.run(
        [
            ffmpeg_binary(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-ss",
            str(seconds),
            "-frames:v",
            "1",
            "-vf",
            "crop=2:2:80:44,scale=1:1:flags=neighbor,format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        timeout=120,
    )
    if len(completed.stdout) < 3:
        raise AssertionError(
            f"无法提取 {path.name} 的 {seconds}s 画面："
            f"{completed.stderr.decode('utf-8', errors='replace')[-500:]}"
        )
    return tuple(completed.stdout[:3])  # type: ignore[return-value]


def _audio_at(path: Path, seconds: float) -> tuple[float, float]:
    """返回短窗口的零交叉频率和 RMS，检查声画接缝没有累计错位。"""

    completed = subprocess.run(
        [
            ffmpeg_binary(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-ss",
            str(seconds),
            "-t",
            "0.35",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        timeout=120,
    )
    samples = array("h")
    samples.frombytes(completed.stdout)
    if not samples:
        raise AssertionError(f"无法提取 {path.name} 的 {seconds}s 音频")
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    crossings = sum(
        1
        for previous, current in zip(samples, samples[1:])
        if (previous < 0 <= current) or (previous >= 0 > current)
    )
    return crossings * 48_000 / (2 * len(samples)), rms


class Stage1MediaIntegrationTests(unittest.TestCase):
    """真实 FFmpeg 合成夹具：中文/空格路径、24/30fps、VFR 与无声素材。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("未找到 FFmpeg/ffprobe")
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name) / "中文 空格 素材"
        cls.root.mkdir()
        _make_colored_media(
            cls.root / "第19集.mp4",
            duration=160,
            color="0xff0000",
            change_to="0xff00ff",
            fps=24,
            audio=True,
            frequency=440,
        )
        _make_colored_media(
            cls.root / "第16集.mp4",
            duration=52,
            color="0x00ff00",
            fps=30,
            audio=True,
            frequency=660,
        )
        _make_colored_media(
            cls.root / "第17集 无声.mp4",
            duration=48,
            color="0x0000ff",
            fps=24,
            audio=False,
        )
        _make_colored_media(
            cls.root / "第18集 VFR.mp4",
            duration=50,
            color="0xffff00",
            fps=30,
            audio=True,
            frequency=990,
            vfr=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_exact_export_preview_recovery_and_source_protection(self) -> None:
        probes = {
            name: probe_media(self.root / name)
            for name in ("第19集.mp4", "第16集.mp4", "第17集 无声.mp4", "第18集 VFR.mp4")
        }
        manifest = {
            "schema_version": 1,
            "example_only": False,
            "drama": "阶段一合成验收",
            "sources": [
                {
                    "file": name,
                    "episode": int(name[1:3]),
                    "expected_duration_ms": probe.duration_us // 1_000,
                }
                for name, probe in probes.items()
            ],
            "cuts": [
                {
                    "title": "19-16-17-18-19",
                    "segments": [
                        {"file": "第19集.mp4", "in_ms": 0, "out_ms": 8_000, "purpose": "钩子"},
                        {"file": "第16集.mp4", "in_ms": 10_000, "out_ms": 52_000, "purpose": "冲突"},
                        {"file": "第17集 无声.mp4", "in_ms": 7_000, "out_ms": 48_000, "purpose": "信息"},
                        {
                            "file": "第18集 VFR.mp4",
                            "in_ms": 11_000,
                            "out_ms": 50_000,
                            "purpose": "升级",
                            "original_audio": "mute",
                        },
                        {"file": "第19集.mp4", "in_ms": 90_000, "out_ms": 155_000, "purpose": "回收"},
                    ],
                }
            ],
        }
        manifest_path = self.root / "剪辑清单.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        source_hashes_before = {
            name: quick_hash(self.root / name) for name in probes
        }

        document = import_manifest(manifest_path, self.root)
        cut = document.active_cut
        self.assertEqual(document.total_duration_us(), 195_000_000)
        document.adjust_segment_end(cut.id, 4, 1_000_000)
        self.assertEqual(document.total_duration_us(), 196_000_000)
        document.undo()
        self.assertEqual(document.total_duration_us(), 195_000_000)

        project_path = self.root / ".local_slice_assistant" / "阶段一.localcut.json"
        save_project(document, project_path)
        reopened = load_project(project_path).document
        self.assertEqual(reopened.total_duration_us(), 195_000_000)

        preview = create_junction_preview(reopened, junction_index=1, margin_ms=500)
        self.assertTrue(preview.output_path.exists())
        self.assertLessEqual(
            abs(preview.observed_duration_us - 1_000_000), preview.frame_duration_us
        )
        self._assert_qt_player_can_play(preview.output_path)

        result = export_cut(reopened)
        self.assertTrue(result.output_path.exists())
        self._assert_qt_player_can_play(result.output_path)
        self.assertLessEqual(
            abs(result.observed_duration_us - 195_000_000),
            result.frame_duration_us,
        )
        output_probe = probe_media(result.output_path)
        self.assertTrue(output_probe.has_audio, "无声源片段应被补静音，成片仍有 AAC 音轨")
        self.assertLessEqual(
            abs(output_probe.duration_us - 195_000_000), result.frame_duration_us
        )

        # 时间轴四个接缝后的颜色验证，同时覆盖首尾画面与无累计漂移。
        first_frame = _pixel_at(result.output_path, 0.001)
        self.assertGreater(first_frame[0], 180)  # precise first frame: red
        green = _pixel_at(result.output_path, 8.5)
        self.assertGreater(green[1], 120)
        self.assertLess(green[0], 100)
        blue = _pixel_at(result.output_path, 50.5)
        self.assertGreater(blue[2], 120)
        self.assertLess(blue[0], 100)
        yellow = _pixel_at(result.output_path, 91.5)
        self.assertGreater(yellow[0], 120)
        self.assertGreater(yellow[1], 120)
        magenta = _pixel_at(result.output_path, 194.5)
        self.assertGreater(magenta[0], 140)
        self.assertGreater(magenta[2], 140)
        self.assertLess(magenta[1], 100)
        last_frame = _pixel_at(result.output_path, 195 - 1 / 24)
        self.assertGreater(last_frame[0], 140)  # precise final output frame: magenta
        self.assertGreater(last_frame[2], 140)

        # 各有声片段的基频在接缝后仍与对应画面一致；无声/静音段不残留原声。
        start_frequency, start_rms = _audio_at(result.output_path, 0.5)
        self.assertGreater(start_rms, 500)
        self.assertAlmostEqual(start_frequency, 440, delta=30)
        green_frequency, green_rms = _audio_at(result.output_path, 8.5)
        self.assertGreater(green_rms, 500)
        self.assertAlmostEqual(green_frequency, 660, delta=35)
        self.assertLess(_audio_at(result.output_path, 50.5)[1], 80)
        self.assertLess(_audio_at(result.output_path, 91.5)[1], 80)
        final_frequency, final_rms = _audio_at(result.output_path, 130.5)
        self.assertGreater(final_rms, 500)
        self.assertAlmostEqual(final_frequency, 440, delta=30)

        self.assertEqual(
            source_hashes_before,
            {name: quick_hash(self.root / name) for name in probes},
            "导出不得修改任一输入素材",
        )
        candidates = discover_media_files(
            self.root, exclusions=resolve_excluded_dirs(self.root)
        )
        self.assertNotIn(result.output_path.resolve(), candidates)
        self.assertNotIn(preview.output_path.resolve(), candidates)

        # VFR 夹具实际生成：avg_frame_rate 与容器 r_frame_rate 不同。
        raw_probe = subprocess.run(
            [
                ffprobe_binary(),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,r_frame_rate",
                "-of",
                "json",
                str(self.root / "第18集 VFR.mp4"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=True,
        )
        stream = json.loads(raw_probe.stdout)["streams"][0]
        self.assertNotEqual(stream["avg_frame_rate"], stream["r_frame_rate"])

    def _assert_qt_player_can_play(self, path: Path) -> None:
        """验证真实 QtMultimedia 后端能加载并推进普通或接缝预览媒体。"""

        from PySide6.QtCore import QEventLoop, QTimer, QUrl
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        player = QMediaPlayer()
        audio = QAudioOutput()
        try:
            player.setAudioOutput(audio)
            statuses: list[QMediaPlayer.MediaStatus] = []
            positions: list[int] = []
            player.mediaStatusChanged.connect(statuses.append)
            player.positionChanged.connect(positions.append)
            player.setSource(QUrl.fromLocalFile(str(path)))
            # Use the application's real event loop rather than a nested loop:
            # this exercises the same Media Foundation scheduling path as GUI use.
            QTimer.singleShot(700, player.play)
            QTimer.singleShot(4_000, app.quit)
            app.exec()
            self.assertNotEqual(player.mediaStatus(), QMediaPlayer.MediaStatus.InvalidMedia)
            self.assertGreater(player.duration(), 0)
            self.assertTrue(
                any(position > 0 for position in positions),
                f"播放器状态：{statuses}",
            )
        finally:
            player.stop()
            player.setSource(QUrl())
            player.setAudioOutput(None)
            player.deleteLater()
            audio.deleteLater()
            release_loop = QEventLoop()
            QTimer.singleShot(150, release_loop.quit)
            release_loop.exec()
            app.processEvents()
