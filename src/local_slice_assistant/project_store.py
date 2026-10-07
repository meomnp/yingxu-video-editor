"""原子保存、恢复副本与用户显式重关联。"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import ManifestValidationError, ProjectLoadError, ProjectSaveError
from .ffmpeg import probe_media
from .models import ProjectDocument, SourceInfo
from .paths import (
    discover_media_files,
    file_identity,
    resolve_excluded_dirs,
    resolve_media_root,
    safe_resolve_media_path,
)


PROJECT_SUFFIX = ".localcut.json"


@dataclass(frozen=True, slots=True)
class LoadedProject:
    document: ProjectDocument
    recovered_from_backup: bool = False


def default_project_path(media_root: str | Path, drama: str) -> Path:
    root = resolve_media_root(media_root)
    safe_title = re.sub(r'[<>:"/\\\\|?*\\x00-\\x1f]+', "_", drama).strip(" ._")
    safe_root = re.sub(r'[<>:"/\\\\|?*\\x00-\\x1f]+', "_", root.name).strip(" ._")
    return (
        root.parent / "映序项目" / f"{safe_root or '未命名素材'}" / "工程"
        / f"{safe_title or '未命名工程'}{PROJECT_SUFFIX}"
    )


def _check_project_path(path: str | Path) -> Path:
    target = Path(path).expanduser()
    if not target.name.endswith(PROJECT_SUFFIX):
        raise ProjectSaveError(
            f"工程文件必须以 {PROJECT_SUFFIX} 结尾，避免误覆盖其他文件。"
        )
    return target


def save_project(document: ProjectDocument, path: str | Path) -> Path:
    """同目录临时文件写完、fsync 后原子替换，并保留一份可读恢复副本。"""

    document.validate()
    target = _check_project_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectSaveError("无法创建工程保存目录。", detail=str(exc)) from exc
    recovery = target.with_name(f"{target.name}.recovery")
    temporary: Path | None = None
    recovery_temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            suffix=".tmp",
            prefix=f".{target.name}.",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Only a readable primary may replace the previous recovery copy. In
        # particular, saving a recovered document must not back up its corrupt
        # primary over the last good copy before the final replacement succeeds.
        recovery_source: Path | None = target
        try:
            _read_project(target)
        except ProjectLoadError:
            try:
                _read_project(recovery)
            except ProjectLoadError:
                recovery_source = temporary
            else:
                recovery_source = None
        if recovery_source is not None:
            with tempfile.NamedTemporaryFile(
                suffix=".tmp",
                prefix=f".{recovery.name}.",
                dir=target.parent,
                delete=False,
            ) as handle:
                recovery_temporary = Path(handle.name)
            # Stage the backup too: a failed or partial copy must not truncate
            # an existing, verified recovery file.
            shutil.copy2(recovery_source, recovery_temporary)
            with recovery_temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(recovery_temporary, recovery)
        os.replace(temporary, target)
        return target
    except PermissionError as exc:
        raise ProjectSaveError(
            "没有写入工程文件的权限。请改选可写的素材文件夹。", detail=str(target)
        ) from exc
    except OSError as exc:
        raise ProjectSaveError("工程保存失败。", detail=str(exc)) from exc
    finally:
        for pending in (temporary, recovery_temporary):
            if pending and pending.exists():
                try:
                    pending.unlink()
                except OSError:
                    pass


def _read_project(path: Path) -> ProjectDocument:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectLoadError("工程文件无法读取或已损坏。", detail=str(path)) from exc
    if not isinstance(raw, dict):
        raise ProjectLoadError("工程文件根节点无效。", detail=str(path))
    try:
        return ProjectDocument.from_dict(raw)
    except (KeyError, TypeError, ValueError, ManifestValidationError) as exc:
        raise ProjectLoadError("工程数据结构无效。", detail=str(exc)) from exc


def _matching_identity(source: SourceInfo, candidate: Path) -> bool:
    try:
        size, _mtime_ns, digest = file_identity(candidate)
    except ProjectLoadError:
        return False
    return size == source.size and digest == source.quick_hash


def _matching_saved_identity(source: SourceInfo, candidate: Path) -> bool:
    try:
        size, mtime_ns, digest = file_identity(candidate)
    except ProjectLoadError:
        return False
    return (
        size == source.size
        and mtime_ns == source.mtime_ns
        and digest == source.quick_hash
    )


def validate_project_sources(document: ProjectDocument, media_root: str | Path) -> None:
    root = resolve_media_root(media_root)
    exclusions = resolve_excluded_dirs(root)
    failures: list[str] = []
    for relative, source in document.sources.items():
        try:
            _safe_relative, absolute = safe_resolve_media_path(
                root, relative, exclusions=exclusions
            )
        except Exception:
            failures.append(relative)
            continue
        if not _matching_saved_identity(source, absolute):
            failures.append(relative)
            continue
        probe = probe_media(absolute)
        if abs(probe.duration_us - source.duration_us) > max(
            source.frame_duration_us, probe.frame_duration_us
        ):
            failures.append(relative)
    if failures:
        raise ProjectLoadError(
            "工程引用的原始素材缺失或已变化；请选择新的素材文件夹进行重关联。",
            detail="；".join(failures[:10]),
        )


def load_project(
    path: str | Path,
    *,
    media_root: str | Path | None = None,
    allow_recovery: bool = True,
    validate_sources: bool = True,
) -> LoadedProject:
    target = _check_project_path(path)
    try:
        document = _read_project(target)
        recovered = False
    except ProjectLoadError:
        recovery = target.with_name(f"{target.name}.recovery")
        if not allow_recovery or not recovery.exists():
            raise
        document = _read_project(recovery)
        recovered = True
    if media_root is not None:
        document.media_root = str(resolve_media_root(media_root))
    if validate_sources:
        validate_project_sources(document, document.media_root)
    return LoadedProject(document=document, recovered_from_backup=recovered)


def relink_project(document: ProjectDocument, media_root: str | Path) -> None:
    """只在用户重新选择根目录后扫描该目录，以指纹安全地找回被重命名的原片。"""

    root = resolve_media_root(media_root)
    exclusions = resolve_excluded_dirs(root)
    candidates = discover_media_files(root, exclusions=exclusions)
    replacements: dict[str, tuple[str, Path]] = {}
    used_paths: set[Path] = set()

    for old_relative, source in document.sources.items():
        direct: Path | None = None
        try:
            _relative, possible = safe_resolve_media_path(
                root, old_relative, exclusions=exclusions
            )
            if _matching_identity(source, possible):
                direct = possible
        except Exception:
            direct = None
        if direct:
            replacements[old_relative] = (old_relative, direct)
            used_paths.add(direct)
            continue

        matches = [
            candidate
            for candidate in candidates
            if candidate not in used_paths and _matching_identity(source, candidate)
        ]
        if len(matches) != 1:
            reason = "找不到" if not matches else "找到多个"
            raise ProjectLoadError(
                f"{reason}可安全重关联的原始素材。",
                detail=old_relative,
            )
        candidate = matches[0]
        relative = candidate.relative_to(root).as_posix()
        probe = probe_media(candidate)
        if abs(probe.duration_us - source.duration_us) > max(
            source.frame_duration_us, probe.frame_duration_us
        ):
            raise ProjectLoadError("重关联候选的媒体时长不一致。", detail=relative)
        replacements[old_relative] = (relative, candidate)
        used_paths.add(candidate)

    new_sources: dict[str, SourceInfo] = {}
    rename_map: dict[str, str] = {}
    for old_relative, source in document.sources.items():
        new_relative, absolute = replacements[old_relative]
        size, mtime_ns, digest = file_identity(absolute)
        source.relative_path = new_relative
        source.size = size
        source.mtime_ns = mtime_ns
        source.quick_hash = digest
        new_sources[new_relative] = source
        rename_map[old_relative] = new_relative
    for cut in document.cuts:
        for segment in cut.segments:
            segment.source_file = rename_map[segment.source_file]
    document.sources = new_sources
    document.media_root = str(root)
    document.validate()
