"""Strict adapter to the already-installed, network-disabled separation worker."""
import json
import os
from pathlib import Path
import subprocess
from time import monotonic
import wave

from .errors import LocalSliceError
from .narration_mix import file_sha256

SEPARATION_ROOT = Path(os.environ.get("LOCAL_SLICE_SEPARATION_ROOT", Path.home() / ".yingxu" / "separation"))
SEPARATION_PYTHON = Path(os.environ.get("LOCAL_SLICE_SEPARATION_PYTHON", SEPARATION_ROOT / "venv" / "Scripts" / "python.exe"))
SEPARATION_CLI = Path(os.environ.get("LOCAL_SLICE_SEPARATION_CLI", SEPARATION_ROOT / "separation_cli.py"))


def validate_separation(input_path, output_dir):
    folder = Path(output_dir).resolve()
    record = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    if record.get("real_separation") is not True or record.get("model_id") != "955717e8":
        raise ValueError("缺少实际本地模型分离回执，不能把静音文件当作背景轨。")
    if record.get("input_sha256") != file_sha256(input_path):
        raise ValueError("分离输入与回执不匹配。")
    with wave.open(str(input_path), "rb") as reader:
        frames, rate = reader.getnframes(), reader.getframerate()
    expected_frames = (frames * 44100 + rate - 1) // rate
    for kind in ("vocals", "background"):
        path = Path(record[kind + "_path"]).resolve()
        if path.parent != folder or file_sha256(path) != record[kind + "_sha256"]:
            raise ValueError("分离轨路径或哈希校验失败。")
        with wave.open(str(path), "rb") as reader:
            if (reader.getnframes(), reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (expected_frames, 44100, 2, 3):
                raise ValueError("分离输出帧数、采样率或声道不匹配，未使用。")
    return record


def separate_audio(input_path, output_dir, progress, cancel, *, timeout_seconds=3600):
    if not SEPARATION_PYTHON.is_file() or not SEPARATION_CLI.is_file():
        raise LocalSliceError("本地人声分离工具不可用，未用静音替代。")
    if Path(output_dir).exists():
        raise LocalSliceError("分离目录已存在，请使用新目录；部分文件不代表已完成。")
    if cancel.is_set():
        raise LocalSliceError("已取消，尚未运行人声分离。")
    process = subprocess.Popen([str(SEPARATION_PYTHON), str(SEPARATION_CLI), "--input", str(Path(input_path).resolve()), "--output-dir", str(Path(output_dir).resolve())],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    start, last_update = monotonic(), 0
    try:
        while True:
            if cancel.is_set() or monotonic() - start > timeout_seconds:
                raise LocalSliceError("已停止本次人声分离，部分输出不会用于混音。")
            if monotonic() - last_update >= 5:
                progress(f"本地CPU人声分离中，已用 {int(monotonic()-start)} 秒；未启动配音推理。")
                last_update = monotonic()
            try:
                stdout, stderr = process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                continue
        events = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except ValueError:
                pass
        if process.returncode != 0 or not any(item.get("event") == "completed" for item in events):
            raise LocalSliceError("本地人声分离失败，未使用不完整结果。", detail=(stdout + stderr)[-2000:])
        return validate_separation(input_path, output_dir)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        process.stdout.close()
        process.stderr.close()
