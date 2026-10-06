"""Offline fixed-region timecode reader using the installed Windows OCR engine."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path


class TimecodeReadError(ValueError):
    pass


def parse_timecode(text: str) -> str:
    # Keep four colon-separated fields. Never guess a frame rate or substitute
    # ambiguous letters with digits; multiple codes means the ROI is too wide.
    normalized = text.replace('：', ':')
    matches = re.findall(r'(?<!\d)(\d{2})\s*:\s*(\d{2})\s*:\s*(\d{2})\s*:\s*(\d{2})(?!\d)', normalized)
    if len(matches) != 1:
        raise TimecodeReadError('未读到唯一时间码，请只框住左侧当前时间，不要包含右侧总时长。识别内容：' + text[:100])
    h, m, s, f = matches[0]
    if int(m) >= 60 or int(s) >= 60:
        raise TimecodeReadError('识别出的时间码格式无效，请重新调整读取框。')
    return ':'.join((h, m, s, f))


def read_timecode(image, *, check_cancel=None) -> str:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    if image.isNull() or image.width() < 20 or image.height() < 6:
        raise TimecodeReadError('时间码读取框太小。')
    # Enlarge the native pixels for the local OCR engine, without reinterpreting
    # the timestamp or deriving it from screen coordinates.
    prepared = image.convertToFormat(QImage.Format.Format_Grayscale8)
    prepared.invertPixels()
    prepared = prepared.scaledToHeight(100, Qt.TransformationMode.SmoothTransformation)
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'read_timecode_windows.ps1'
    with tempfile.TemporaryDirectory(prefix='jianying_timecode_') as directory:
        path = Path(directory) / 'timecode.png'
        if not prepared.save(str(path)):
            raise TimecodeReadError('无法准备时间码识别图片。')
        # Execute the shipped source as an in-memory command; do not change the
        # machine's script execution policy or request administrator privileges.
        command = "& {" + script.read_text(encoding='utf-8') + "} -ImagePath '" + str(path).replace("'", "''") + "'"
        process = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        import time
        deadline = time.monotonic() + 15
        try:
            while True:
                if check_cancel:
                    check_cancel()
                try:
                    out, err = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() > deadline:
                        raise TimecodeReadError('本地时间码识别超时。')
            if process.returncode:
                raise TimecodeReadError('本地时间码识别失败：' + err.decode('utf-8', errors='replace')[:300])
            return parse_timecode(json.loads(out.decode('utf-8-sig'))['text'])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
