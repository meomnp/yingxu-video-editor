"""只缓存可再生成的局部基础分析结果。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .paths import is_within, resolve_media_root


CACHE_DIRECTORY = ".local_slice_assistant"
_MANAGED_CACHE_DIRS = {
    "analysis_cache": {".json"},
    "vision_cache": {".json"},
    "previews": {".mp4"},
    "proxies": {".mp4"},
}


def project_cache_root(media_root: str | Path) -> Path:
    root = resolve_media_root(media_root)
    return (root / CACHE_DIRECTORY).resolve(strict=False)


def _managed_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, suffixes in _MANAGED_CACHE_DIRS.items():
        candidate = root / directory
        try:
            entries = candidate.glob("*")
        except OSError:
            continue
        for item in entries:
            try:
                if (
                    item.is_file()
                    and item.suffix.casefold() in suffixes
                    and is_within(item.resolve(strict=False), root)
                ):
                    files.append(item)
            except OSError:
                continue
    return files


def _vision_work_dirs(root: Path) -> list[Path]:
    """返回本工具遗留的视觉临时目录，绝不沿链接离开工程缓存根。"""

    try:
        resolved_root = root.resolve(strict=False)
        entries = root.glob("vision-*")
    except OSError:
        return []
    directories: list[Path] = []
    for item in entries:
        try:
            resolved = item.resolve(strict=False)
            if (
                item.is_dir()
                and item.parent.resolve(strict=False) == resolved_root
                and is_within(resolved, resolved_root)
            ):
                directories.append(item)
        except OSError:
            continue
    return directories


def enforce_project_cache_limit(media_root: str | Path, limit_bytes: int) -> None:
    """按最久未用淘汰本工具可再生成的缓存，正式导出目录从不参与。"""

    root = project_cache_root(media_root)
    try:
        files = _managed_files(root)
    except OSError:
        return
    total = sum(item.stat().st_size for item in files if item.exists())
    for item in sorted(files, key=lambda entry: entry.stat().st_mtime):
        if total <= max(0, int(limit_bytes)):
            break
        try:
            size = item.stat().st_size
            item.unlink()
            total -= size
        except OSError:
            continue


def clear_project_cache(media_root: str | Path) -> int:
    """只清理本工具自身的分析、预览、代理和遗留视觉临时文件。"""

    removed = 0
    root = project_cache_root(media_root)
    for item in _managed_files(root):
        try:
            item.unlink()
            removed += 1
        except OSError:
            continue
    # 正常完成的画面任务会立即删除自己的帧目录。这里仅处理崩溃或强制退出时
    # 遗留在工程缓存根下、且已严格验证没有借链接逃出该根的目录。
    for directory in _vision_work_dirs(root):
        try:
            removed += sum(1 for child in directory.rglob("*") if child.is_file())
            shutil.rmtree(directory)
        except OSError:
            continue
    return removed


class AnalysisCache:
    def __init__(
        self,
        media_root: str | Path,
        *,
        limit_bytes: int,
        namespace: str = "analysis_cache",
    ) -> None:
        if namespace not in _MANAGED_CACHE_DIRS or ".json" not in _MANAGED_CACHE_DIRS[namespace]:
            raise ValueError("unsupported analysis cache namespace")
        self.media_root = str(resolve_media_root(media_root))
        self.namespace = namespace
        self.root = (project_cache_root(self.media_root) / namespace).resolve(
            strict=False
        )
        self.limit_bytes = max(0, int(limit_bytes))

    @staticmethod
    def key_for(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _path_for(self, key: str) -> Path:
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
            raise ValueError("invalid analysis cache key")
        return self.root / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path_for(key)
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, payload: dict[str, Any]) -> None:
        if self.limit_bytes <= 0:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path_for(key)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.root,
                prefix=f".{key}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.utime(target, None)
            self.enforce_limit()
        finally:
            if temporary and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def enforce_limit(self) -> None:
        enforce_project_cache_limit(self.media_root, self.limit_bytes)

    def clear_expired(self, older_than_seconds: int) -> None:
        cutoff = time.time() - max(0, older_than_seconds)
        for item in self.root.glob("*.json"):
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
            except OSError:
                continue
