"""接缝前后的小片段预览。"""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Callable
from uuid import uuid4

from .analysis_cache import enforce_project_cache_limit
from .errors import ManifestValidationError
from .exporter import ExportResult, ExportSettings, export_cut
from .models import Cut, ProjectDocument
from .resources import ResourceMeter


def create_junction_preview(
    document: ProjectDocument,
    *,
    cut_id: str | None = None,
    junction_index: int,
    margin_ms: int = 1_000,
    settings: ExportSettings | None = None,
    cache_limit_bytes: int = 2 * 1024 * 1024 * 1024,
    cache_directory: str = "previews",
    cancel_event: Event | None = None,
    progress: Callable[[str], None] | None = None,
    resource_meter: ResourceMeter | None = None,
) -> ExportResult:
    """生成“前段尾部 + 后段头部”预览，时间码仍来自当前工程状态。"""

    if margin_ms <= 0:
        raise ManifestValidationError("接缝预览长度必须大于零。")
    cut = document.get_cut(cut_id or document.active_cut.id)
    if not 0 <= junction_index < len(cut.segments) - 1:
        raise ManifestValidationError("请选择两个相邻片段之间的接缝。")
    margin_us = margin_ms * 1_000
    before = cut.segments[junction_index].copy()
    after = cut.segments[junction_index + 1].copy()
    before.in_us = max(before.in_us, before.out_us - margin_us)
    after.out_us = min(after.out_us, after.in_us + margin_us)
    preview_cut = Cut(title=f"{cut.title} 接缝预览", segments=[before, after])
    preview_document = ProjectDocument(
        media_root=document.media_root,
        drama=document.drama,
        original_manifest=document.original_manifest,
        sources=document.sources,
        cuts=[preview_cut],
        resource_settings=dict(document.resource_settings),
    )
    if cache_directory not in {"previews", "proxies"}:
        raise ManifestValidationError("接缝缓存目录无效。")
    root = Path(document.media_root)
    target = (
        root
        / ".local_slice_assistant"
        / cache_directory
        / f"junction_{uuid4().hex}.mp4"
    )
    result = export_cut(
        preview_document,
        output_path=target,
        settings=settings,
        cancel_event=cancel_event,
        progress=progress,
        internal_cache=True,
        internal_cache_directory=cache_directory,
        resource_meter=resource_meter,
    )
    enforce_project_cache_limit(document.media_root, cache_limit_bytes)
    return result
