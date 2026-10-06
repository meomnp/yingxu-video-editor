"""剪辑清单 v1 的严格导入器。"""

from __future__ import annotations

import copy
import re
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Any, Callable

from .errors import ExportCancelled, ManifestValidationError
from .ffmpeg import probe_media
from .models import Cut, ProjectDocument, Segment, SourceInfo
from .paths import MEDIA_SUFFIXES, discover_media_files, file_identity, resolve_excluded_dirs, resolve_media_root, safe_resolve_media_path
from .plan_response import read_plan_response


def _natural_media_key(path: Path) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.as_posix()))


def create_project_from_folder(
    folder: str | Path,
    *,
    on_progress: Callable[[str], None] | None = None,
    cancel_event: Event | None = None,
) -> ProjectDocument:
    """直接把选定文件夹的视频按文件名顺序放入一个手动剪辑工程。"""

    root = resolve_media_root(folder)
    files = sorted(
        discover_media_files(root, exclusions=resolve_excluded_dirs(root)),
        key=lambda path: _natural_media_key(path.relative_to(root)),
    )
    if not files:
        raise ManifestValidationError("所选文件夹没有可用的视频文件。")
    sources: dict[str, SourceInfo] = {}
    segments: list[Segment] = []
    for index, source in enumerate(files, start=1):
        if cancel_event and cancel_event.is_set():
            raise ExportCancelled("已取消建立多视频工程；没有修改原视频。")
        probe = probe_media(source)
        if probe.width <= 0 or probe.height <= 0:
            raise ManifestValidationError("素材没有可用的视频画面。", detail=str(source))
        relative = source.relative_to(root).as_posix()
        size, mtime_ns, digest = file_identity(source)
        sources[relative] = SourceInfo(
            relative_path=relative,
            episode=None,
            expected_duration_us=probe.duration_us,
            duration_us=probe.duration_us,
            size=size,
            mtime_ns=mtime_ns,
            quick_hash=digest,
            has_audio=probe.has_audio,
            fps_num=probe.fps_num,
            fps_den=probe.fps_den,
            width=probe.width,
            height=probe.height,
        )
        segments.append(Segment(source_file=relative, in_us=0, out_us=probe.duration_us, title=source.stem))
        if on_progress:
            on_progress(f"已读取 {index}/{len(files)} 个视频：{relative}")
    return ProjectDocument(
        media_root=str(root),
        drama=root.name,
        original_manifest={"source_mode": "manual_folder", "source_files": list(sources)},
        sources=sources,
        cuts=[Cut(title="手动剪辑", segments=segments)],
    )


def create_project_from_videos(
    video_paths: list[str | Path],
    *,
    on_progress: Callable[[str], None] | None = None,
    cancel_event: Event | None = None,
) -> ProjectDocument:
    """把用户多选的同一文件夹视频直接建成工程，不扫描同目录其他视频。"""

    if not video_paths:
        raise ManifestValidationError("请至少选择一条视频。")
    files = [Path(item).expanduser().resolve(strict=True) for item in video_paths]
    root = resolve_media_root(files[0].parent)
    if any(path.parent != root for path in files):
        raise ManifestValidationError("快速多选导入目前要求视频位于同一文件夹。")
    if any(not path.is_file() or path.suffix.casefold() not in MEDIA_SUFFIXES for path in files):
        raise ManifestValidationError("快速多选导入只支持 MP4、MOV、MKV 等视频文件。")
    sources: dict[str, SourceInfo] = {}
    segments: list[Segment] = []
    for index, source in enumerate(files, start=1):
        if cancel_event and cancel_event.is_set():
            raise ExportCancelled("已取消快速导入；没有修改原视频。")
        probe = probe_media(source)
        if probe.width <= 0 or probe.height <= 0:
            raise ManifestValidationError("素材没有可用的视频画面。", detail=str(source))
        relative = source.relative_to(root).as_posix()
        if relative in sources:
            continue
        size, mtime_ns, digest = file_identity(source)
        sources[relative] = SourceInfo(relative_path=relative, episode=None, expected_duration_us=probe.duration_us,
            duration_us=probe.duration_us, size=size, mtime_ns=mtime_ns, quick_hash=digest,
            has_audio=probe.has_audio, fps_num=probe.fps_num, fps_den=probe.fps_den,
            width=probe.width, height=probe.height)
        segments.append(Segment(source_file=relative, in_us=0, out_us=probe.duration_us, title=source.stem))
        if on_progress:
            on_progress(f"已读取 {index}/{len(files)} 个指定视频：{relative}")
    return ProjectDocument(media_root=str(root), drama=root.name,
        original_manifest={"source_mode": "selected_videos", "source_files": list(sources)},
        sources=sources, cuts=[Cut(title="快速导入", segments=segments)])


def create_project_from_video(video_path: str | Path) -> ProjectDocument:
    """把用户选定的一条现成视频作为完整、可编辑的初始切片。"""

    source = Path(video_path).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in MEDIA_SUFFIXES:
        raise ManifestValidationError("请选择 MP4、MOV、MKV 等受支持的视频文件。")
    if source.stat().st_size == 0:
        raise ManifestValidationError("所选视频是空文件，无法建立工程。")
    probe = probe_media(source)
    if probe.width <= 0 or probe.height <= 0:
        raise ManifestValidationError("所选文件没有可用的视频画面。")
    root = resolve_media_root(source.parent)
    relative, _absolute = safe_resolve_media_path(root, source.name)
    size, mtime_ns, digest = file_identity(source)
    drama = source.stem
    segment = Segment(
        source_file=relative,
        in_us=0,
        out_us=probe.duration_us,
        title="现成视频（完整保留）",
        purpose="可在此基础上调整、配音和包装",
    )
    cut = Cut(title=drama, segments=[segment])
    return ProjectDocument(
        media_root=str(root),
        drama=drama,
        original_manifest={"source_mode": "single_video", "source_file": relative},
        sources={
            relative: SourceInfo(
                relative_path=relative,
                episode=None,
                expected_duration_us=probe.duration_us,
                duration_us=probe.duration_us,
                size=size,
                mtime_ns=mtime_ns,
                quick_hash=digest,
                has_audio=probe.has_audio,
                fps_num=probe.fps_num,
                fps_den=probe.fps_den,
                width=probe.width,
                height=probe.height,
            )
        },
        cuts=[cut],
    )


def _field(raw: dict[str, Any], name: str, expected: type) -> Any:
    value = raw.get(name)
    if not isinstance(value, expected) or (expected is str and not value.strip()):
        label = "非空文本" if expected is str else expected.__name__
        raise ManifestValidationError(f"字段 {name} 必须是{label}。")
    return value


def _integer(value: object, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(f"字段 {field_name} 必须是整数毫秒。")
    if minimum is not None and value < minimum:
        raise ManifestValidationError(f"字段 {field_name} 不能小于 {minimum}。")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestValidationError(f"字段 {field_name} 必须是文本或省略。")
    return value.strip() or None


def _drama_tag(drama: str) -> str:
    return "#" + drama.strip().strip("《》").strip()


def _only_fields(raw: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ManifestValidationError(
            f"{location} 含未支持字段：{', '.join(unknown)}。",
            detail="未导入方案。请让 AI 使用应用导出的模板；不会静默忽略速度、转场等额外指令。",
        )


def _publishing(raw: object, drama: str) -> dict[str, Any] | None:
    """校验并保存可复制的发布字段，不替网页端编写任何文案。"""

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestValidationError("publishing 必须是对象或省略。")
    _only_fields(raw, {"kind", "title", "body", "tags"}, "publishing")
    kind = raw.get("kind", "video")
    if kind not in ("video", "graphic"):
        raise ManifestValidationError("publishing.kind 只能是 video 或 graphic。")
    title = _field(raw, "title", str).strip()
    body = _field(raw, "body", str).strip()
    tags = raw.get("tags")
    if (
        not isinstance(tags, list)
        or not 6 <= len(tags) <= 8
        or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
    ):
        raise ManifestValidationError(
            "publishing.tags 必须是 6—8 个非空标签。"
        )
    tags = [tag.strip() for tag in tags]
    expected_first = "#漫剧榜单推荐" if kind == "graphic" else _drama_tag(drama)
    if tags[0] != expected_first:
        raise ManifestValidationError(
            "发布字段首标签不符合当前规则。",
            detail=f"{kind} 首标签应为 {expected_first}，实际为 {tags[0]}。",
        )
    return {"kind": kind, "title": title, "body": body, "tags": tags}


def import_manifest(
    manifest_path: str | Path,
    media_root: str | Path,
) -> ProjectDocument:
    """导入 v1 清单并用 ffprobe 验证每一条实际媒体映射。"""

    return _import_manifest_data(read_plan_response(manifest_path), media_root)


def _import_manifest_data(raw: dict[str, Any], media_root: str | Path) -> ProjectDocument:
    _only_fields(raw, {"schema_version", "example_only", "drama", "sources", "cuts", "planning_package_id"}, "方案顶层")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ManifestValidationError("只支持 schema_version: 1 的剪辑清单。")
    if "planning_package_id" in raw:
        _field(raw, "planning_package_id", str)
    if raw.get("example_only") is not False:
        raise ManifestValidationError(
            "示例清单不能导入。请让网页规划结果导出 example_only: false 的正式清单。"
        )
    drama = _field(raw, "drama", str).strip()
    source_entries = raw.get("sources")
    cut_entries = raw.get("cuts")
    if not isinstance(source_entries, list) or not source_entries:
        raise ManifestValidationError("sources 必须是非空数组。")
    if not isinstance(cut_entries, list) or not cut_entries:
        raise ManifestValidationError("cuts 必须是非空数组。")

    resolved_root = resolve_media_root(media_root)
    exclusions = resolve_excluded_dirs(resolved_root)
    resolved_sources: dict[str, tuple[Path, int | None, int]] = {}
    file_names: dict[str, str] = {}
    for entry_index, entry in enumerate(source_entries, start=1):
        if not isinstance(entry, dict):
            raise ManifestValidationError(f"sources[{entry_index}] 必须是对象。")
        _only_fields(entry, {"file", "episode", "expected_duration_ms"}, f"sources[{entry_index}]")
        relative, absolute = safe_resolve_media_path(
            resolved_root, entry.get("file"), exclusions=exclusions
        )
        if relative in resolved_sources:
            raise ManifestValidationError("sources 中出现重复的素材路径。", detail=relative)
        basename_key = PurePosixPath(relative).name.casefold()
        previous = file_names.get(basename_key)
        if previous:
            raise ManifestValidationError(
                "不同文件夹内存在同名素材，当前清单不允许歧义映射。",
                detail=f"{previous} / {relative}",
            )
        file_names[basename_key] = relative

        episode = entry.get("episode")
        if episode is not None:
            episode = _integer(episode, f"sources[{entry_index}].episode", minimum=1)
        expected_ms = _integer(
            entry.get("expected_duration_ms"),
            f"sources[{entry_index}].expected_duration_ms",
            minimum=1,
        )
        resolved_sources[relative] = (absolute, episode, expected_ms)

    cuts: list[Cut] = []
    for cut_index, raw_cut in enumerate(cut_entries, start=1):
        if not isinstance(raw_cut, dict):
            raise ManifestValidationError(f"cuts[{cut_index}] 必须是对象。")
        _only_fields(raw_cut, {"title", "segments", "publishing", "narration"}, f"cuts[{cut_index}]")
        title = _field(raw_cut, "title", str).strip()
        raw_segments = raw_cut.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ManifestValidationError(f"cuts[{cut_index}].segments 必须是非空数组。")
        segments: list[Segment] = []
        for segment_index, raw_segment in enumerate(raw_segments, start=1):
            if not isinstance(raw_segment, dict):
                raise ManifestValidationError(
                    f"cuts[{cut_index}].segments[{segment_index}] 必须是对象。"
                )
            _only_fields(raw_segment, {"file", "in_ms", "out_ms", "title", "purpose", "original_audio", "first_line", "last_line"},
                         f"cuts[{cut_index}].segments[{segment_index}]")
            source_file = raw_segment.get("file")
            if not isinstance(source_file, str):
                raise ManifestValidationError("片段 file 必须是素材相对路径。")
            source_file = source_file.strip().replace("\\", "/")
            if source_file not in resolved_sources:
                raise ManifestValidationError(
                    "片段引用了 sources 未登记的素材。", detail=source_file
                )
            in_ms = _integer(raw_segment.get("in_ms"), "片段 in_ms", minimum=0)
            out_ms = _integer(raw_segment.get("out_ms"), "片段 out_ms", minimum=0)
            if out_ms <= in_ms:
                raise ManifestValidationError("片段 out_ms 必须大于 in_ms。")
            audio_policy = raw_segment.get("original_audio", "keep")
            if audio_policy not in ("keep", "mute"):
                raise ManifestValidationError("original_audio 只能是 keep 或 mute。")
            segments.append(
                Segment(
                    source_file=source_file,
                    in_us=in_ms * 1_000,
                    out_us=out_ms * 1_000,
                    title=_optional_string(raw_segment.get("title"), "片段 title")
                    or f"{title} - {segment_index}",
                    purpose=_optional_string(raw_segment.get("purpose"), "purpose"),
                    original_audio=audio_policy,
                    first_line=_optional_string(raw_segment.get("first_line"), "first_line"),
                    last_line=_optional_string(raw_segment.get("last_line"), "last_line"),
                )
            )
        cut = Cut(
                title=title,
                segments=segments,
                publishing=_publishing(raw_cut.get("publishing"), drama),
            )
        if "narration" in raw_cut:
            from .narration_plan import import_narration
            raw_narration = raw_cut["narration"]
            if isinstance(raw_narration, dict):
                _only_fields(raw_narration, {"schema_version", "time_basis", "timeline_fingerprint", "cues"},
                             f"cuts[{cut_index}].narration")
                raw_cues = raw_narration.get("cues")
                if isinstance(raw_cues, list):
                    for cue_index, cue in enumerate(raw_cues, start=1):
                        if isinstance(cue, dict):
                            _only_fields(cue, {"id", "text", "start_ms", "end_ms", "original_audio", "background_gain_db", "audio_filename"},
                                         f"cuts[{cut_index}].narration.cues[{cue_index}]")
            try:
                cut.packaging["narration_plan"] = import_narration(
                    raw_narration, cut, allow_bind_current=True
                )
            except ValueError as exc:
                raise ManifestValidationError(f"{title}：{exc}") from exc
        cuts.append(cut)

    # Validate every instruction first. A later bad segment must not trigger
    # ffprobe or hide behind a failure while opening an earlier source.
    sources: dict[str, SourceInfo] = {}
    for relative, (absolute, episode, expected_ms) in resolved_sources.items():
        probe = probe_media(absolute)
        expected_us = expected_ms * 1_000
        if abs(probe.duration_us - expected_us) > probe.frame_duration_us:
            raise ManifestValidationError(
                "清单声明时长与实际媒体时长不符；请回到规划端校正，工具不会自动缩放时间码。",
                detail=f"{relative}: 清单 {expected_ms} ms，实际 {probe.duration_us // 1000} ms",
            )
        size, mtime_ns, digest = file_identity(absolute)
        sources[relative] = SourceInfo(
            relative_path=relative, episode=episode, expected_duration_us=expected_us,
            duration_us=probe.duration_us, size=size, mtime_ns=mtime_ns, quick_hash=digest,
            has_audio=probe.has_audio, fps_num=probe.fps_num, fps_den=probe.fps_den,
            width=probe.width, height=probe.height,
        )

    return ProjectDocument(
        media_root=str(resolved_root),
        drama=drama,
        original_manifest=copy.deepcopy(raw),
        sources=sources,
        cuts=cuts,
    )


def import_planned_manifest(
    manifest_path: str | Path, previous: ProjectDocument
) -> ProjectDocument:
    """Import a web plan for this source set, retaining transcript context.

    The returned document is independent; this never changes the previous project
    or writes to media. Unknown files are rejected before probing any media.
    """
    raw = read_plan_response(manifest_path)
    expected_package = previous.planning_context.get("package_id")
    if expected_package and raw.get("planning_package_id") != expected_package:
        raise ManifestValidationError(
            "AI 方案不对应当前任务包，请让网页 AI 原样返回当前 package_id 为 planning_package_id。",
            detail="未替换工程；请检查是否导入了旧方案或漏了任务包编号。",
        )
    if raw.get("drama") != previous.drama:
        raise ManifestValidationError("AI 方案剧名与当前工程不一致，请导入这批素材对应的方案。")
    entries = raw.get("sources")
    if not isinstance(entries, list) or not entries:
        raise ManifestValidationError("AI 方案缺少 sources 素材目录。")
    for entry in entries:
        name = entry.get("file") if isinstance(entry, dict) else None
        if not isinstance(name, str) or name.replace("\\", "/") not in previous.sources:
            raise ManifestValidationError("AI 方案引用了本批次没有导入的视频。", detail=str(name))
    # Import the exact data checked above; do not reread a file which may have
    # changed between the package/source binding check and media validation.
    imported = _import_manifest_data(raw, previous.media_root)
    requested = previous.planning_context.get("form_options", {})
    if "count" in requested and len(imported.cuts) != requested["count"]:
        raise ManifestValidationError("AI 方案条数与本次要求不符，未替换工程。",
                                      detail=f"要求 {requested['count']} 条，返回 {len(imported.cuts)} 条。")
    if "narration_count" in requested:
        narrated = sum(bool(cut.packaging.get("narration_plan", {}).get("cues")) for cut in imported.cuts)
        if narrated != requested["narration_count"]:
            raise ManifestValidationError("AI 方案的解说条数与本次要求不符，未替换工程。",
                                          detail=f"要求解说 {requested['narration_count']} 条，返回 {narrated} 条。")
    for name, source in imported.sources.items():
        original = previous.sources[name]
        if (source.size, source.quick_hash, source.duration_us) != (
            original.size, original.quick_hash, original.duration_us
        ):
            raise ManifestValidationError("素材在方案设计后发生变化，请重新导入素材并设计。", detail=name)
        if source.episode != original.episode:
            raise ManifestValidationError("AI 方案修改了素材集数，请按任务包原样返回。", detail=name)
    # Keep the full original source catalog: unused episodes may still have
    # registered transcripts and must remain available for later replanning.
    imported.sources = copy.deepcopy(previous.sources)
    imported.transcript_paths = list(previous.transcript_paths)
    imported.transcript_bindings = dict(previous.transcript_bindings)
    imported.transcript_overrides = dict(previous.transcript_overrides)
    imported.resource_settings = copy.deepcopy(previous.resource_settings)
    imported.planning_context = dict(previous.planning_context)
    imported.planning_context["imported_plan"] = str(Path(manifest_path).resolve())
    imported.validate()
    return imported
