"""Use the voice project's existing runtime/client; never load another model."""
import json
import os
from pathlib import Path
from queue import Queue, Empty
import subprocess
from threading import Thread
from time import monotonic

from .errors import LocalSliceError

VOICE_ROOT = Path(os.environ.get("LOCAL_SLICE_VOICE_ROOT", Path.home() / ".yingxu" / "voice"))
VOICE_PYTHON = Path(os.environ.get("LOCAL_SLICE_VOICE_PYTHON", VOICE_ROOT / "runtime" / "Scripts" / "python.exe"))
VOICE_CLI = Path(os.environ.get("LOCAL_SLICE_VOICE_CLI", VOICE_ROOT / "voice_bridge_cli.py"))
# Never select or publish a machine owner's private voice profile by default.
NARRATOR_ONE = "unconfigured"
VOICE_EXCHANGE_CLI = VOICE_CLI.with_name("studio_exchange_cli.py")


def default_voice_profile():
    return dict(id=NARRATOR_ONE, name="未配置音色", review_scope="unverified",
                review_label="未配置本地声音工具；请自行配置或导入音频")


def load_voice_profiles():
    """Read local, validated profile metadata; no server, synthesis or model startup."""
    if not VOICE_PYTHON.is_file() or not VOICE_EXCHANGE_CLI.is_file():
        raise LocalSliceError("声音工作台的档案读取入口不存在，请检查本地安装。")
    try:
        result = subprocess.run([str(VOICE_PYTHON), str(VOICE_EXCHANGE_CLI), "capabilities"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        data = json.loads(result.stdout)
        if result.returncode or data.get("ok") is not True:
            raise ValueError(data.get("error", "本地档案读取失败"))
        payload = data["result"]
        profiles, ids = [], set()
        for row in payload["profiles"]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
                raise ValueError("声音档案编号无效")
            if row["id"] in ids or not isinstance(row.get("name"), str) or not row["name"].strip():
                raise ValueError("声音档案名称或编号重复")
            ids.add(row["id"])
            # A reviewed sample is not approval of every future generated sentence.
            scope = row.get("review_scope", "unverified")
            label = "已有试听通过样例 · 新台词仍需试听" if scope == "reviewed_sample_only" else "待核对音色听审"
            profiles.append(dict(id=row["id"], name=row["name"], review_scope=scope, review_label=label))
        return profiles, [str(item) for item in payload.get("warnings", [])]
    except subprocess.TimeoutExpired as exc:
        raise LocalSliceError("读取本地声音档案超时，原选择保持不变；可稍后刷新。") from exc
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LocalSliceError(f"读取本地声音档案失败，原选择保持不变：{exc}") from exc


class VoiceBridgeError(LocalSliceError):
    def __init__(self, message, *, terminal=False):
        super().__init__(message)
        self.terminal = terminal


def run_voice_bridge(request_path, output_dir, profile_id, progress, cancel, *, recover=False, timeout_seconds=1800):
    if not VOICE_PYTHON.is_file() or not VOICE_CLI.is_file():
        raise LocalSliceError("本地配音运行环境或桥接脚本不存在；请打开声音工作台检查安装。")
    args = [str(VOICE_PYTHON), str(VOICE_CLI), "--request", str(request_path),
            "--voice-profile-id", profile_id, "--output-dir", str(output_dir)]
    if recover:
        args.append("--recover")
    if cancel.is_set():
        raise LocalSliceError("已取消，尚未提交配音任务。")
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding="utf-8", errors="replace", text=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    queue = Queue()

    def consume():
        try:
            for line in process.stdout:
                queue.put(line)
        finally:
            queue.put(None)

    reader = Thread(target=consume, daemon=True)
    reader.start()
    started = monotonic()
    terminal = None
    terminal_failure = False
    last_message = "无法连接本地声音服务；请先打开声音工作台。"
    try:
        while True:
            if cancel.is_set() or monotonic() - started > timeout_seconds:
                raise LocalSliceError("已停止等待；声音服务可能仍在生成，未停止模型、未重复提交。请保留工程，稍后点击找回配音结果。")
            try:
                line = queue.get(timeout=0.2)
            except Empty:
                continue
            if line is None:
                break
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("event")
            if kind in ("submitted", "waiting"):
                progress("本地配音已提交，正在等待生成；取消仅停止等待，之后可找回结果。")
            elif kind == "completed":
                terminal = event
            elif kind == "not_ready":
                last_message = f"原配音任务尚未完成（{event.get('state', '未知状态')}），未重新生成；稍后再次找回。"
            elif kind in ("error", "package_rejected", "detached"):
                last_message = str(event.get("message", "配音未完成"))
                terminal_failure = kind == "package_rejected" or event.get("state") in ("failed", "package_rejected", "interrupted_unverified")
        code = process.wait(timeout=5)
        if code != 0 or terminal is None:
            raise VoiceBridgeError(last_message, terminal=terminal_failure)
        result_path = Path(terminal.get("package", "")).resolve()
        if result_path.parent != Path(output_dir).resolve() or not result_path.is_file():
            raise LocalSliceError("声音端没有返回预期目录内的配音包。")
        return result_path
    finally:
        # This is only the client child, never the shared voice server.
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        reader.join(timeout=2)
        process.stdout.close()
