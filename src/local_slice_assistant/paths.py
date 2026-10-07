"""受控素材路径、输出隔离与轻量指纹。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

from .errors import ManifestValidationError, ProjectLoadError


MEDIA_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm"}
DEFAULT_EXCLUDED_DIR_NAMES = {
    "本地切片助手导出",
    "映序导出",
    "映序项目",
    ".local_slice_assistant",
    "outputs",
    "output",
    "cache",
}


def normalize_relative_media_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestValidationError("素材 file 必须是非空相对路径。")
    text = raw.strip().replace("\\", "/")
    win_path = PureWindowsPath(text)
    posix_path = PurePosixPath(text)
    if (
        win_path.is_absolute()
        or posix_path.is_absolute()
        or win_path.drive
        or text.startswith("//")
    ):
        raise ManifestValidationError("素材 file 不能是绝对路径。", detail=raw)
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ManifestValidationError(
            "素材 file 不能包含空层级、. 或 ..。", detail=raw
        )
    return "/".join(parts)


def resolve_media_root(root: str | Path) -> Path:
    candidate = Path(root).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProjectLoadError("素材文件夹不存在或无法访问。", detail=str(candidate)) from exc
    if not resolved.is_dir():
        raise ProjectLoadError("素材根路径必须是文件夹。", detail=str(resolved))
    return resolved


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_excluded_dirs(root: Path, extra: Iterable[str | Path] = ()) -> list[Path]:
    exclusions: list[Path] = []
    for name in DEFAULT_EXCLUDED_DIR_NAMES:
        path = root / name
        try:
            exclusions.append(path.resolve(strict=False))
        except OSError:
            continue
    for entry in extra:
        path = Path(entry)
        if not path.is_absolute():
            path = root / path
        try:
            exclusions.append(path.resolve(strict=False))
        except OSError:
            continue
    return exclusions


def safe_resolve_media_path(
    root: str | Path,
    raw_relative_path: object,
    *,
    exclusions: Iterable[Path] = (),
) -> tuple[str, Path]:
    resolved_root = resolve_media_root(root)
    relative = normalize_relative_media_path(raw_relative_path)
    candidate = resolved_root.joinpath(*relative.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ManifestValidationError("清单引用的素材文件不存在。", detail=relative) from exc
    if not resolved.is_file() or not is_within(resolved, resolved_root):
        raise ManifestValidationError(
            "素材路径越出所选文件夹或不是普通文件。", detail=relative
        )
    for excluded in exclusions:
        if is_within(resolved, excluded):
            raise ManifestValidationError(
                "不能把工具的导出或缓存目录当作源素材。", detail=relative
            )
    return relative, resolved


def quick_hash(path: str | Path, block_size: int = 65_536) -> str:
    """只对首尾块做指纹，用于重命名重关联，不替代完整性校验。"""

    file_path = Path(path)
    digest = hashlib.sha256()
    try:
        size = file_path.stat().st_size
        with file_path.open("rb") as handle:
            digest.update(handle.read(block_size))
            if size > block_size:
                handle.seek(max(0, size - block_size))
                digest.update(handle.read(block_size))
    except OSError as exc:
        raise ProjectLoadError("无法读取素材指纹。", detail=str(file_path)) from exc
    digest.update(str(size).encode("ascii"))
    return digest.hexdigest()


def file_identity(path: str | Path) -> tuple[int, int, str]:
    file_path = Path(path)
    try:
        stat = file_path.stat()
    except OSError as exc:
        raise ProjectLoadError("无法读取素材文件属性。", detail=str(file_path)) from exc
    return stat.st_size, stat.st_mtime_ns, quick_hash(file_path)


def discover_media_files(
    root: str | Path, *, exclusions: Iterable[Path] = ()
) -> list[Path]:
    """只用于用户明确选择文件夹后的重关联；不会默认扫描任何素材盘。"""

    resolved_root = resolve_media_root(root)
    resolved_exclusions = list(exclusions)
    found: list[Path] = []
    for candidate in resolved_root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not is_within(resolved, resolved_root):
            continue
        if any(is_within(resolved, excluded) for excluded in resolved_exclusions):
            continue
        found.append(resolved)
    return sorted(found, key=lambda item: os.fspath(item).casefold())
