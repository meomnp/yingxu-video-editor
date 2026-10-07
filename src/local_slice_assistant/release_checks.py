"""Release-only checks for the separate FFmpeg/ffprobe executables."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


class ReleaseCheckError(RuntimeError):
    pass


def validate_ffmpeg_text(license_text: str, buildconf: str, encoders: str, target: str) -> None:
    """Reject GPL/nonfree builds and require the tested native H.264 encoder."""

    if target not in {"windows", "macos"}:
        raise ReleaseCheckError("目标平台必须是 windows 或 macos。")
    combined_license = license_text.casefold()
    if (re.search(r"gnu general public license|(?<![\w-])gpl(?:-|\s|$)", combined_license)
            or re.search(r"--enable-(?:gpl|nonfree)\b", buildconf, re.IGNORECASE)
            or re.search(r"--enable-libx26[45]\b", buildconf, re.IGNORECASE)):
        raise ReleaseCheckError("检测到 GPL/nonfree FFmpeg 配置；拒绝打包。")
    if not re.search(r"gnu lesser general public license|\blgpl(?:-|\s|$)", combined_license):
        raise ReleaseCheckError("FFmpeg 没有声明 LGPL 许可证；拒绝打包。")
    for required in ("--enable-libfreetype", "--enable-libharfbuzz"):
        if required not in buildconf:
            raise ReleaseCheckError(f"FFmpeg 缺少所需构建项 {required}；拒绝打包。")
    platform_flag, encoder = (
        ("--enable-mediafoundation", "h264_mf")
        if target == "windows"
        else ("--enable-videotoolbox", "h264_videotoolbox")
    )
    if platform_flag not in buildconf:
        raise ReleaseCheckError(f"FFmpeg 缺少平台构建项 {platform_flag}；拒绝打包。")
    if not re.search(rf"\b{re.escape(encoder)}\b", encoders):
        raise ReleaseCheckError(f"FFmpeg 缺少平台 H.264 编码器 {encoder}；拒绝打包。")


def _run(binary: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [str(binary), *arguments], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseCheckError(f"无法检查媒体工具 {binary.name}（{type(exc).__name__}）。") from None
    output = result.stdout + result.stderr
    if result.returncode:
        raise ReleaseCheckError(f"媒体工具检查失败：{binary.name} {arguments[0]}。")
    return output


def validate_portable_ffmpeg(target: str, ffmpeg: str | Path, ffprobe: str | Path) -> None:
    ffmpeg_path, ffprobe_path = Path(ffmpeg), Path(ffprobe)
    for binary in (ffmpeg_path, ffprobe_path):
        if not binary.is_file():
            raise ReleaseCheckError(f"找不到媒体工具：{binary}")
    license_text = _run(ffmpeg_path, "-hide_banner", "-L")
    buildconf = _run(ffmpeg_path, "-hide_banner", "-buildconf")
    encoders = _run(ffmpeg_path, "-hide_banner", "-encoders")
    # ffprobe prints its license through its banner handler; -hide_banner
    # suppresses -L too. Do not mistake empty output for an unknown license.
    probe_license = _run(ffprobe_path, "-L")
    if re.search(r"gnu general public license|(?<![\w-])gpl(?:-|\s|$)|--enable-nonfree|--enable-gpl",
                 probe_license, re.IGNORECASE):
        raise ReleaseCheckError("ffprobe 检测到 GPL/nonfree 许可证标记；拒绝打包。")
    if not re.search(r"gnu lesser general public license|\blgpl(?:-|\s|$)",
                     probe_license, re.IGNORECASE):
        raise ReleaseCheckError("ffprobe 没有声明 LGPL 许可证；拒绝打包。")
    validate_ffmpeg_text(license_text, buildconf, encoders, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("windows", "macos"))
    parser.add_argument("ffmpeg", type=Path)
    parser.add_argument("ffprobe", type=Path)
    args = parser.parse_args(argv)
    try:
        validate_portable_ffmpeg(args.target, args.ffmpeg, args.ffprobe)
    except ReleaseCheckError as exc:
        print(f"Release check failed: {exc}", file=sys.stderr)
        return 1
    print(f"Release check passed: {args.target} LGPL FFmpeg/ffprobe and native H.264 encoder.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
