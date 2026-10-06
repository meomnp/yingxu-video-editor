"""FFmpeg / ffprobe 的最小封装。

所有调用使用参数数组，不经过 shell；进度工作只能由后台线程调用。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable, Sequence

from .errors import ExportCancelled, ExportError, MediaProbeError
from .resources import ResourceMeter


@dataclass(frozen=True, slots=True)
class MediaProbe:
    duration_us: int
    has_audio: bool
    fps_num: int
    fps_den: int
    width: int
    height: int

    @property
    def frame_duration_us(self) -> int:
        if self.fps_num > 0 and self.fps_den > 0:
            return max(1, round(1_000_000 * self.fps_den / self.fps_num))
        return 40_000


def _binary(name: str) -> str:
    custom = os.environ.get(f"LOCAL_SLICE_{name.upper()}")
    candidates: list[str | None] = [custom, shutil.which(name)]
    if getattr(sys, "frozen", False):
        # PyInstaller 6 onedir releases keep add-binary files under _internal;
        # older layouts use the executable directory. Support both.
        bundle_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        candidates.append(os.fspath(bundle_dir / f"{name}.exe"))
        candidates.append(os.fspath(Path(sys.executable).resolve().parent / f"{name}.exe"))
    candidate = next((item for item in candidates if item and Path(item).is_file()), None)
    if not candidate:
        raise MediaProbeError(
            f"找不到 {name}。请安装 FFmpeg，或设置 LOCAL_SLICE_{name.upper()}。",
        )
    return candidate


def ffmpeg_binary() -> str:
    return _binary("ffmpeg")


def ffprobe_binary() -> str:
    return _binary("ffprobe")


def _decimal_seconds_to_us(value: object) -> int:
    try:
        return int(
            (Decimal(str(value)) * Decimal(1_000_000)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
    except (InvalidOperation, ValueError) as exc:
        raise MediaProbeError("FFprobe 返回了无效的媒体时长。", detail=str(value)) from exc


def _parse_rate(value: object) -> tuple[int, int]:
    text = str(value or "0/1")
    try:
        fraction = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return 0, 1
    if fraction <= 0:
        return 0, 1
    return fraction.numerator, fraction.denominator


def probe_media(path: str | Path) -> MediaProbe:
    """读取真实媒体容器信息；不解码整片，也不分析内容。"""

    media_path = Path(path)
    command = [
        ffprobe_binary(),
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,width,height,avg_frame_rate,r_frame_rate,duration",
        "-show_streams",
        "-of",
        "json",
        os.fspath(media_path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise MediaProbeError("无法启动 ffprobe。", detail=str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError("ffprobe 检测媒体超时。", detail=str(media_path)) from exc
    if completed.returncode != 0:
        raise MediaProbeError(
            "无法读取素材媒体信息。",
            detail=completed.stderr.strip()[-1200:] or os.fspath(media_path),
        )
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError("ffprobe 返回了无法解析的信息。") from exc

    streams = raw.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video:
        raise MediaProbeError("素材没有可导出的画面流。", detail=os.fspath(media_path))
    duration_value = (raw.get("format") or {}).get("duration")
    if duration_value in (None, "N/A"):
        duration_value = video.get("duration")
    duration_us = _decimal_seconds_to_us(duration_value)
    if duration_us <= 0:
        raise MediaProbeError("素材时长必须大于零。", detail=os.fspath(media_path))
    fps_num, fps_den = _parse_rate(
        video.get("avg_frame_rate") or video.get("r_frame_rate")
    )
    return MediaProbe(
        duration_us=duration_us,
        has_audio=any(item.get("codec_type") == "audio" for item in streams),
        fps_num=fps_num,
        fps_den=fps_den,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
    )


def probe_audio_duration(path: str | Path) -> int:
    """读取用户显式选择的音频时长，不要求它带视频流。"""

    media_path = Path(path)
    command = [
        ffprobe_binary(),
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-show_streams",
        "-of",
        "json",
        os.fspath(media_path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise MediaProbeError("无法启动 ffprobe。", detail=str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError("ffprobe 检测音频超时。", detail=str(media_path)) from exc
    if completed.returncode != 0:
        raise MediaProbeError(
            "无法读取配音／音乐媒体信息。",
            detail=completed.stderr.strip()[-1200:] or os.fspath(media_path),
        )
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError("ffprobe 返回了无法解析的音频信息。") from exc
    if not any(item.get("codec_type") == "audio" for item in raw.get("streams") or []):
        raise MediaProbeError("所选文件没有可用音频流。", detail=os.fspath(media_path))
    duration = _decimal_seconds_to_us((raw.get("format") or {}).get("duration"))
    if duration <= 0:
        raise MediaProbeError("配音／音乐时长必须大于零。", detail=os.fspath(media_path))
    return duration


def run_ffmpeg(arguments: Sequence[str], *, cancel_event=None, progress=None,
               timeout_seconds=None, resource_meter=None, expected_duration_us=None) -> None:
    """Keep large filter graphs outside the Windows command-line limit."""
    from tempfile import TemporaryDirectory
    options = dict(cancel_event=cancel_event, progress=progress,
                   timeout_seconds=timeout_seconds, resource_meter=resource_meter,
                   expected_duration_us=expected_duration_us)
    args = list(arguments)
    if "-filter_complex" in args and sum(len(str(arg)) + 3 for arg in args) > 16000:
        with TemporaryDirectory(prefix="localcut-filter-") as folder:
            index = args.index("-filter_complex")
            graph = Path(folder) / "graph.txt"
            graph.write_text(args[index + 1], encoding="utf-8")
            args[index:index + 2] = ["-/filter_complex", str(graph)]
            return _run_ffmpeg_process(args, **options)
    return _run_ffmpeg_process(args, **options)


class _FFmpegReport:
    """Drain stderr continuously with bounded diagnostics, never block the encoder."""

    def __init__(self):
        self.lock = Lock()
        self.latest = {}
        self.errors = deque(maxlen=40)

    def read(self, stream):
        fields = {}
        keys = {"frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms",
                "out_time", "dup_frames", "drop_frames", "speed", "progress"}
        for line in stream:
            key, separator, value = line.strip().partition("=")
            if separator and (key in keys or (key.startswith("stream_") and key.endswith("_q"))):
                fields[key] = value.strip()
                if key == "progress":
                    with self.lock:
                        self.latest = fields.copy()
                    fields.clear()
            else:
                with self.lock:
                    self.errors.append(line[-512:])

    def snapshot(self):
        with self.lock:
            return self.latest.copy(), "".join(self.errors)[-2000:]


def _clock_text(microseconds):
    tenths = max(0, int(microseconds)) // 100000
    seconds, tenth = divmod(tenths, 10)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02}:{minute:02}:{second:02}.{tenth}"


def _encoding_status(fields, expected_duration_us, idle_seconds):
    try:
        encoded = max(0, int(fields.get("out_time_us", 0)))
    except (TypeError, ValueError):
        encoded = 0
    status = "FFmpeg 已编码 " + _clock_text(encoded)
    if expected_duration_us and expected_duration_us > 0:
        percent = min(99.9, 100 * encoded / expected_duration_us)
        status += f" / {_clock_text(expected_duration_us)}（{percent:.1f}%）"
    speed = fields.get("speed")
    if speed and speed != "N/A":
        status += f"；速度 {speed}"
    if expected_duration_us and expected_duration_us > 0 and idle_seconds < 8:
        try:
            rate = float(str(speed).removesuffix("x"))
        except (TypeError, ValueError):
            rate = 0
        if 0 < rate < float("inf") and 0 < encoded < expected_duration_us:
            remaining = (expected_duration_us - encoded) / rate
            status += f"；预计编码剩余约 {_clock_text(remaining)}（随速度变化，不含校验）"
        elif encoded >= expected_duration_us:
            status += "；正在收尾，尚未确认导出成功"
        else:
            status += "；剩余时间正在估算"
    if idle_seconds >= 8:
        status += f"；已 {int(idle_seconds)} 秒未报告新的编码进度，进程仍在运行，可取消"
    elif not fields:
        status = "FFmpeg 正在启动／读取素材，等待首次编码进度…"
    return status


def _stop_owned_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _run_ffmpeg_process(
    arguments: Sequence[str],
    *,
    cancel_event: Event | None = None,
    progress: Callable[[str], None] | None = None,
    timeout_seconds: float | None = None,
    resource_meter: ResourceMeter | None = None,
    expected_duration_us: int | None = None,
) -> None:
    """运行一个可取消的 FFmpeg 子进程。"""

    if cancel_event and cancel_event.is_set():
        raise ExportCancelled("导出已取消。")
    # stderr carries both bounded diagnostics and FFmpeg's structured progress.
    # stdout can be a null/raw media sink for internal callers, never parse it.
    command = [ffmpeg_binary(), "-hide_banner", "-nostats", "-loglevel", "warning"]
    if progress:
        command += ["-progress", "pipe:2", "-stats_period", "0.5"]
    command.extend(arguments)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise ExportError("无法启动 FFmpeg。", detail=str(exc)) from exc

    report = _FFmpegReport()
    reader = Thread(target=report.read, args=(process.stderr,), daemon=True)
    reader.start()
    started = last_advance = time.monotonic()
    last_sample = None
    next_report = started
    poll_interval = 0.05 if resource_meter else 0.2
    try:
        while process.poll() is None:
            now = time.monotonic()
            if resource_meter:
                resource_meter.observe(process.pid)
            if cancel_event and cancel_event.is_set():
                raise ExportCancelled("导出已取消，临时文件已停止写入。")
            if timeout_seconds and now - started > timeout_seconds:
                raise ExportError("导出超时，已停止 FFmpeg。")
            if progress and now >= next_report:
                fields, _ = report.snapshot()
                sample = fields.get("out_time_us")
                if sample is not None and sample != last_sample:
                    last_sample, last_advance = sample, now
                progress(_encoding_status(fields, expected_duration_us, now - last_advance))
                next_report = now + (2 if now - last_advance >= 8 else .5)
            try:
                process.wait(timeout=poll_interval)
            except subprocess.TimeoutExpired:
                pass
        reader.join(timeout=5)
        if resource_meter:
            resource_meter.observe(process.pid)
        if cancel_event and cancel_event.is_set():
            raise ExportCancelled("导出已取消。")
        fields, detail = report.snapshot()
        if process.returncode != 0:
            raise ExportError("FFmpeg 导出失败。", detail=detail.strip())
        if progress:
            progress(_encoding_status(fields, expected_duration_us, 0))
            progress("FFmpeg 编码结束，正在校验输出…")
    finally:
        # Also covers a disposed UI callback or resource observer throwing.
        # Never leave an orphan encoder writing after the job is marked stopped.
        _stop_owned_process(process)
        reader.join(timeout=5)
        process.stderr.close()
