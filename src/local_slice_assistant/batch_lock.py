"""Cross-process batch ownership with conservative recovery after a dead owner.

The small guard file is deliberately never deleted: unlinking an advisory-lock
file creates a race where two processes can lock different underlying files.
The legacy production.lock marker remains compatible with older launchers.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from uuid import uuid4


def _process_running(pid):
    """Return False only with OS evidence; denied/unknown must not unlock work."""
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            state = kernel.WaitForSingleObject(handle, 0)
            return {0: False, 258: True}.get(state)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None
    return True


def _lock_guard(stream, *, release=False):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _read_marker(path):
    if path.is_symlink() or path.stat().st_size > 4096:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        return None
    if type(raw) is int:  # Versions before crash-safe recovery stored a PID only.
        return {"pid": raw}
    if isinstance(raw, dict) and raw.get("version") == 2:
        return raw
    return None


@contextmanager
def batch_execution_lock(directory, progress=None):
    directory = Path(directory)
    marker = directory / "production.lock"
    guard = directory / "production.guard"
    if guard.is_symlink():
        raise ValueError("批次执行锁路径无效，未提交任务。")
    with guard.open("a+b") as stream:
        # Byte-range locking works beyond EOF on Windows; a real byte keeps
        # the persistent coordination file easy to inspect on every platform.
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        try:
            _lock_guard(stream)
        except OSError as exc:
            raise ValueError("该批次已有执行锁，正在另一个窗口制作，未重复提交。") from exc
        token = uuid4().hex
        owned = False
        try:
            if marker.exists():
                previous = _read_marker(marker)
                if previous is None or _process_running(previous.get("pid")) is not False:
                    raise ValueError(f"该批次已有执行锁；持有进程仍在运行或无法确认已退出，未重复提交：{marker}")
                marker.unlink()
                if progress:
                    progress("已确认上次批次进程退出，正在恢复原批次；已提交的配音仍只找回，不重复生成。")
            try:
                with marker.open("x", encoding="utf-8") as owner:
                    json.dump(dict(version=2, pid=os.getpid(), token=token), owner)
                    owner.flush()
                    os.fsync(owner.fileno())
                owned = True
            except FileExistsError as exc:
                # An older executable may have acquired its existence lock.
                raise ValueError("该批次已有执行锁，未重复提交。") from exc
            yield
        finally:
            try:
                if owned and marker.exists():
                    current = _read_marker(marker)
                    if current and current.get("token") == token:
                        marker.unlink()
            finally:
                _lock_guard(stream, release=True)

