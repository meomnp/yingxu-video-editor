"""Use the encoders actually present in the selected media-tool build."""
from functools import lru_cache
from pathlib import Path
import re
import subprocess
import sys

from .errors import ExportError
from .ffmpeg import ffmpeg_binary


@lru_cache(maxsize=8)
def _encoders(binary, size, modified):
    try:
        result = subprocess.run([binary, '-hide_banner', '-encoders'], capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError('无法检查视频编码器，请检查随包媒体工具。') from exc
    if result.returncode:
        raise ExportError('媒体工具无法列出视频编码器。')
    return frozenset(re.findall(r'^\s*V\S{5}\s+(\S+)', result.stdout, re.MULTILINE))


def video_encoding_options(*, crf=18, preset='medium'):
    binary = ffmpeg_binary()
    info = Path(binary).stat()
    available = _encoders(binary, info.st_size, info.st_mtime_ns)
    if 'libx264' in available:
        return ['-c:v', 'libx264', '-preset', preset, '-crf', str(crf)]
    if sys.platform == 'win32' and 'h264_mf' in available:
        return ['-c:v', 'h264_mf', '-b:v', '8M']
    if sys.platform == 'darwin' and 'h264_videotoolbox' in available:
        return ['-c:v', 'h264_videotoolbox', '-allow_sw', '1', '-b:v', '8M']
    raise ExportError('当前媒体工具缺少适用于本系统的 H.264 编码器，未生成成片。')
