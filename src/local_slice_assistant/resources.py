"""本地基础模式的资源预算与可测量快照。"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass


GIB = 1024 * 1024 * 1024


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    mode: str
    cpu_threads: int
    preview_long_edge: int
    cache_limit_bytes: int


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    process_working_set_bytes: int | None
    available_memory_bytes: int | None
    gpu_note: str


@dataclass(frozen=True, slots=True)
class ResourceMeasurement:
    duration_seconds: float
    peak_process_working_set_bytes: int | None
    peak_child_working_set_bytes: int | None
    minimum_available_memory_bytes: int | None
    gpu_note: str
    peak_gpu_memory_bytes: int | None = None
    gpu_released: bool | None = None
    peak_gpu_device_used_bytes: int | None = None
    gpu_device_used_after_exit_bytes: int | None = None


def policy_for_mode(mode: str) -> ResourcePolicy:
    if mode == "saver":
        return ResourcePolicy(
            mode="saver",
            cpu_threads=2,
            preview_long_edge=720,
            cache_limit_bytes=2 * GIB,
        )
    return ResourcePolicy(
        mode="standard",
        cpu_threads=6,
        preview_long_edge=1280,
        cache_limit_bytes=2 * GIB,
    )


def process_working_set_bytes(pid: int) -> int | None:
    """只查询本工具的主进程或其已知子进程；不可得时返回 None。"""

    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        is_current = int(pid) == os.getpid()
        handle = (
            kernel32.GetCurrentProcess()
            if is_current
            else kernel32.OpenProcess(0x0400 | 0x1000, False, int(pid))
        )
        if not handle:
            return None
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
            if psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), ctypes.sizeof(counters)
            ):
                return int(counters.WorkingSetSize)
            return None
        finally:
            if not is_current:
                kernel32.CloseHandle(handle)
    except OSError:
        return None


def _windows_memory_snapshot() -> tuple[int | None, int | None]:
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    available: int | None = None
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        available = int(status.ullAvailPhys)
    return process_working_set_bytes(os.getpid()), available


def snapshot() -> ResourceSnapshot:
    """只测量本进程工作集；GPU 无模型阶段明确标为未采集。"""

    if os.name == "nt":
        try:
            working_set, available = _windows_memory_snapshot()
            return ResourceSnapshot(
                process_working_set_bytes=working_set,
                available_memory_bytes=available,
                gpu_note="基础模式未加载视觉模型；未采集 GPU 进程占用。",
            )
        except OSError:
            pass
    return ResourceSnapshot(
        process_working_set_bytes=None,
        available_memory_bytes=None,
        gpu_note="当前平台未采集 GPU 进程占用。",
    )


def gpu_process_memory_bytes(pid: int) -> int | None:
    """读取明确由本工具启动的 CUDA 计算进程占用，单位为字节。

    只按 PID 归属采样，既不把其他软件的占用算进来，也不尝试管理其他程序。
    ``0`` 表示 nvidia-smi 可用且没有找到这个 PID；``None`` 表示当前无法采集。
    """

    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode:
        return None
    for line in completed.stdout.splitlines():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) < 2:
            continue
        try:
            belongs_to_pid = int(fields[0]) == int(pid)
        except ValueError:
            continue
        if not belongs_to_pid:
            continue
        try:
            return max(0, int(float(fields[1])) * 1024 * 1024)
        except ValueError:
            # Windows WDDM frequently lists a compute process but reports
            # [N/A] rather than a trustworthy per-process allocation.
            return None
    return 0


def gpu_device_used_bytes() -> int | None:
    """读取显卡总占用；它可能包含其他软件，绝不冒充本工具独占显存。"""

    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode:
        return None
    values: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            values.append(max(0, int(float(line.strip())) * 1024 * 1024))
        except ValueError:
            continue
    return max(values) if values else None


class ResourceMeter:
    """任务级采样器，记录主进程和该任务明确启动的 FFmpeg/ffprobe 子进程。"""

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._peak_process: int | None = None
        self._peak_child: int | None = None
        self._minimum_available: int | None = None
        self._gpu_note = "基础模式未加载视觉模型；未采集 GPU 进程占用。"
        self._visual_pid: int | None = None
        self._peak_gpu: int | None = None
        self._peak_gpu_device: int | None = None
        self._gpu_device_after_exit: int | None = None
        self._gpu_released: bool | None = None
        self._last_gpu_sample = 0.0

    def track_visual_process(self, pid: int) -> None:
        """声明后续 GPU 采样只针对这个视觉子进程。"""

        self._visual_pid = int(pid)
        self._gpu_note = "正在采样本工具启动的视觉进程 GPU 占用。"

    def observe(self, child_pid: int | None = None, *, force_gpu: bool = False) -> None:
        current = snapshot()
        if self._visual_pid is None:
            self._gpu_note = current.gpu_note
        if current.process_working_set_bytes is not None:
            self._peak_process = max(
                self._peak_process or 0, current.process_working_set_bytes
            )
        if current.available_memory_bytes is not None:
            self._minimum_available = (
                current.available_memory_bytes
                if self._minimum_available is None
                else min(self._minimum_available, current.available_memory_bytes)
            )
        if child_pid is not None:
            child = process_working_set_bytes(child_pid)
            if child is not None:
                self._peak_child = max(self._peak_child or 0, child)
        if self._visual_pid is not None:
            now = time.monotonic()
            if force_gpu or now - self._last_gpu_sample >= 0.4:
                self._last_gpu_sample = now
                gpu_bytes = gpu_process_memory_bytes(self._visual_pid)
                device_bytes = gpu_device_used_bytes()
                if device_bytes is not None:
                    self._peak_gpu_device = max(
                        self._peak_gpu_device or 0, device_bytes
                    )
                if gpu_bytes is None:
                    self._gpu_note = "视觉进程已启动，但当前无法从 nvidia-smi 采集其 GPU 占用。"
                elif gpu_bytes > 0:
                    self._peak_gpu = max(self._peak_gpu or 0, gpu_bytes)
                    self._gpu_note = "正在采样本工具启动的视觉进程 GPU 占用。"

    def mark_visual_process_exited(self, pid: int) -> None:
        """在 wait 后核验该模型 PID 已不再出现在 GPU 计算进程列表。"""

        if self._visual_pid != int(pid):
            return
        self.observe(force_gpu=True)
        remaining = gpu_process_memory_bytes(int(pid))
        self._gpu_device_after_exit = gpu_device_used_bytes()
        if remaining is None:
            self._gpu_released = None
            self._gpu_note = "视觉进程已退出；当前无法从 nvidia-smi 复核显存回落。"
        elif remaining == 0:
            self._gpu_released = True
            self._gpu_note = "视觉进程已退出，nvidia-smi 已确认该 PID 不再占用计算显存。"
        else:
            self._gpu_released = False
            self._gpu_note = "视觉进程已退出，但 nvidia-smi 仍显示该 PID 的显存占用；请稍后复核。"

    def finish(self) -> ResourceMeasurement:
        self.observe()
        return ResourceMeasurement(
            duration_seconds=time.monotonic() - self._started,
            peak_process_working_set_bytes=self._peak_process,
            peak_child_working_set_bytes=self._peak_child,
            minimum_available_memory_bytes=self._minimum_available,
            gpu_note=self._gpu_note,
            peak_gpu_memory_bytes=self._peak_gpu,
            gpu_released=self._gpu_released,
            peak_gpu_device_used_bytes=self._peak_gpu_device,
            gpu_device_used_after_exit_bytes=self._gpu_device_after_exit,
        )


def should_warn_memory(resource: ResourceSnapshot) -> bool:
    return bool(
        resource.available_memory_bytes is not None
        and resource.available_memory_bytes < 8 * GIB
    )
