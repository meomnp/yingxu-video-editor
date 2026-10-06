"""Bounded polling of our encoder only; never manage other applications."""
from pathlib import Path
import shutil
from time import monotonic

from .errors import ExportError
from .resources import GIB, process_working_set_bytes, snapshot

MIB = 1024 * 1024


class ExportResourceGuard:
    """Soft guard, not a hard OS memory reservation or a media-capacity promise."""

    def __init__(self, directory, mode, meter=None):
        self.directory = Path(directory)
        self.meter = meter
        self.next_check = 0.0
        available = snapshot().available_memory_bytes
        ceiling = (2 if mode == 'saver' else 4) * GIB
        self.limit = min(ceiling, max(256 * MIB, available * 3 // 5)) if available is not None else ceiling
        self.observe(None)

    def observe(self, child_pid):
        if self.meter:
            self.meter.observe(child_pid)
        now = monotonic()
        if now < self.next_check:
            return
        self.next_check = now + .5
        available = snapshot().available_memory_bytes
        if available is not None and available < 256 * MIB:
            raise ExportError('系统可用内存不足 256 MiB，已停止本次导出；原片和工程未改动。',
                detail='请先释放内存，或选择省资源模式、减少单次片段数量后重试。不会关闭其他程序。')
        used = process_working_set_bytes(child_pid) if child_pid is not None else None
        if used is not None and used > self.limit:
            raise ExportError('本次编码占用超过内存保护阈值，已停止；未输出不完整成片。',
                detail=f'编码进程 {used // MIB} MiB，保护阈值 {self.limit // MIB} MiB。'
                       '请降低输出分辨率或减少单次片段数量后重试；工程和原片保留。')
        try:
            free = shutil.disk_usage(self.directory).free
        except OSError as exc:
            raise ExportError('无法继续检查导出磁盘，已停止；请检查磁盘连接与权限后重试。') from exc
        if free < 64 * MIB:
            raise ExportError('导出过程中磁盘剩余不足 64 MiB，已停止并清理本次临时成片。',
                              detail='请释放空间后重新导出；不会删除原片、工程或以前的成片。')

    def description(self):
        return f'资源保护：编码进程内存软阈值 {self.limit // MIB} MiB；每 0.5 秒检查内存与导出磁盘，可取消。'
