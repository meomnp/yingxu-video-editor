"""Sibling project storage for API history and text-only API results."""

from __future__ import annotations

from pathlib import Path
import re

from .batches import safe_name
from .paths import resolve_media_root


def api_artifact_directory(
    media_root: str | Path,
    *,
    create: bool = False,
    batch_directory: str | Path | None = None,
) -> Path:
    """Return the project-wide or active-batch ``AI材料`` directory.

    Without ``batch_directory``, this is the stable project-wide location for
    API history. Model replies and executable plans should pass the active
    batch directory so every editing run keeps its own materials. Nothing is
    written into the folder recursively scanned for source videos. Resolve and
    validate every level so a pre-existing symlink cannot redirect writes.
    """

    root = resolve_media_root(media_root)
    parent = root.parent.resolve(strict=True)
    projects = parent / "映序项目"
    project = projects / safe_name(root.name)
    if batch_directory is None:
        artifacts = project / "AI材料"
    else:
        batch = Path(batch_directory).expanduser()
        resolved_batch = batch.resolve(strict=False)
        if (not batch.is_absolute() or resolved_batch.parent != project.resolve(strict=False)
                or not re.search(r"_第\d+批$", resolved_batch.name)):
            raise ValueError("API 方案只能保存在当前素材对应的剪辑批次内。")
        artifacts = resolved_batch / "AI材料"

    if create:
        projects.mkdir(exist_ok=True)
        project.mkdir(exist_ok=True)
        artifacts.mkdir(exist_ok=True)

    resolved_projects = projects.resolve(strict=False)
    resolved_project = project.resolve(strict=False)
    resolved_artifacts = artifacts.resolve(strict=False)
    if resolved_projects.parent != parent:
        raise ValueError("映序项目目录不能通过链接离开素材文件夹的上级目录。")
    if resolved_project.parent != resolved_projects:
        raise ValueError("素材对应的项目目录不能通过链接离开映序项目目录。")
    if batch_directory is None and resolved_artifacts.parent != resolved_project:
        raise ValueError("AI材料目录不能通过链接离开当前项目目录。")
    if batch_directory is not None:
        resolved_batch = Path(batch_directory).resolve(strict=False)
        if resolved_batch.parent != resolved_project or resolved_artifacts.parent != resolved_batch:
            raise ValueError("批次 AI材料目录不能通过链接离开当前剪辑批次。")
    return resolved_artifacts


def api_saved_file_path(
    media_root: str | Path,
    entry: dict,
    *,
    batch_directory: str | Path | None = None,
) -> Path | None:
    """Resolve a recorded reply path, keeping history-file paths in-scope.

    New history entries contain an absolute ``saved_path``. Older entries only
    have a basename, so look in the active batch, project-wide AI材料, then the
    known legacy AI剪辑任务包 folder.
    """

    root = resolve_media_root(media_root)
    project_root = root.parent / "映序项目" / safe_name(root.name)
    allowed_roots = [project_root.resolve(strict=False), (root / "AI剪辑任务包").resolve(strict=False)]
    raw_path = entry.get("saved_path") if isinstance(entry, dict) else None
    if isinstance(raw_path, str) and raw_path.strip():
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        resolved = candidate.resolve(strict=False)
        if any(resolved.is_relative_to(allowed) for allowed in allowed_roots):
            return resolved
        return None

    saved_file = entry.get("saved_file") if isinstance(entry, dict) else None
    if not isinstance(saved_file, str) or not saved_file or Path(saved_file).name != saved_file:
        return None
    candidates = []
    if batch_directory:
        candidates.append(Path(batch_directory) / "AI材料" / saved_file)
    candidates.extend((project_root / "AI材料" / saved_file, root / "AI剪辑任务包" / saved_file))
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if any(resolved.is_relative_to(allowed) for allowed in allowed_roots) and resolved.is_file():
            return resolved
    return (project_root / "AI材料" / saved_file).resolve(strict=False)
