"""Standalone, local fixed-watermark repair. Never mutates the input."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from .ffmpeg import ffmpeg_binary, probe_media
from .video_encoding import video_encoding_options


@dataclass(frozen=True)
class WatermarkRegion:
    x: int
    y: int
    width: int
    height: int

    def validate(self, frame_width: int, frame_height: int) -> None:
        if min(self.x, self.y) < 0 or min(self.width, self.height) < 4:
            raise ValueError("水印区域无效：宽和高至少 4 像素。")
        if self.x + self.width > frame_width or self.y + self.height > frame_height:
            raise ValueError("水印区域超出了视频画面。")


def _filter(region: WatermarkRegion, start: float, end: float) -> str:
    return (
        f"delogo=x={region.x}:y={region.y}:w={region.width}:h={region.height}:"
        f"enable='between(t,{start:.3f},{end:.3f})'"
    )


def preview_frame(source: Path, second: float, region: WatermarkRegion | None = None) -> bytes:
    info = probe_media(source)
    if not 0 <= second < info.duration_us / 1_000_000:
        raise ValueError("预览时间必须位于视频时长内。")
    command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-ss", f"{second:.3f}", "-i", os.fspath(source)]
    if region is not None:
        region.validate(info.width, info.height)
        command += ["-vf", _filter(region, 0, 1)]
    command += ["-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
    result = subprocess.run(command, capture_output=True, timeout=60)
    if result.returncode or not result.stdout:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-500:] or "无法生成预览。")
    return result.stdout


def suggest_output(source: Path) -> Path:
    folder = source.parent / "去水印导出"
    stem = source.stem + "_去水印"
    candidate = folder / f"{stem}.mp4"
    index = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{index}.mp4"
        index += 1
    return candidate


def remove_watermark(
    source: Path,
    output: Path,
    region: WatermarkRegion,
    start: float,
    end: float,
    *,
    status: Callable[[str], None] | None = None,
    cancel: Event | None = None,
) -> Path:
    source, output = source.resolve(), output.resolve()
    if source == output:
        raise ValueError("不能覆盖原视频。请选择另一个输出文件。")
    if not source.is_file():
        raise FileNotFoundError(source)
    info = probe_media(source)
    region.validate(info.width, info.height)
    duration = info.duration_us / 1_000_000
    if not (0 <= start < end <= duration + 0.05):
        raise ValueError("去水印起止时间不在视频时长内。")
    if output.exists():
        raise FileExistsError("输出文件已存在，请换一个文件名。")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".part.mp4")
    if temporary.exists():
        raise FileExistsError(f"上次临时文件仍在，请先检查：{temporary}")
    command = [
        ffmpeg_binary(), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", os.fspath(source), "-map", "0:v:0", "-map", "0:a?",
        "-vf", _filter(region, start, end), *video_encoding_options(),
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
        "-progress", "pipe:1", "-y", os.fspath(temporary),
    ]
    try:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace") as process:
            assert process.stdout is not None
            for line in process.stdout:
                if cancel is not None and cancel.is_set():
                    process.terminate()
                    process.wait(timeout=10)
                    raise RuntimeError("已取消去水印，原视频未改变。")
                if status and line.startswith("out_time_ms="):
                    try:
                        # FFmpeg's out_time_ms is historically microseconds.
                        progress = min(100, int(int(line.split("=", 1)[1]) / info.duration_us * 100))
                        status(f"正在去水印：{progress}%")
                    except ValueError:
                        pass
            error = process.stderr.read()[-1200:] if process.stderr else ""
            if process.wait() != 0:
                raise RuntimeError(error or "FFmpeg 去水印失败。")
        if cancel is not None and cancel.is_set():
            raise RuntimeError("已取消去水印，原视频未改变。")
        result = probe_media(temporary)
        if abs(result.duration_us - info.duration_us) > max(250_000, info.frame_duration_us * 2):
            raise RuntimeError("输出时长与原视频不一致，已放弃结果。")
        if info.has_audio and not result.has_audio:
            raise RuntimeError("输出音轨丢失，已放弃结果。")
        temporary.replace(output)
        return output
    finally:
        if temporary.exists():
            temporary.unlink()
