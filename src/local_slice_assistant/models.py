"""工程、素材和片段的数据模型。

外部协议统一使用毫秒整数；内部统一使用整数微秒，避免浮点累计误差。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .errors import ManifestValidationError


def new_id() -> str:
    return uuid4().hex


@dataclass(slots=True)
class SourceInfo:
    relative_path: str
    episode: int | None
    expected_duration_us: int
    duration_us: int
    size: int
    mtime_ns: int
    quick_hash: str
    has_audio: bool
    fps_num: int
    fps_den: int
    width: int
    height: int

    @property
    def frame_duration_us(self) -> int:
        if self.fps_num > 0 and self.fps_den > 0:
            return max(1, round(1_000_000 * self.fps_den / self.fps_num))
        return 40_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "episode": self.episode,
            "expected_duration_us": self.expected_duration_us,
            "duration_us": self.duration_us,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "quick_hash": self.quick_hash,
            "has_audio": self.has_audio,
            "fps_num": self.fps_num,
            "fps_den": self.fps_den,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourceInfo":
        return cls(
            relative_path=str(raw["relative_path"]),
            episode=raw.get("episode"),
            expected_duration_us=int(raw["expected_duration_us"]),
            duration_us=int(raw["duration_us"]),
            size=int(raw["size"]),
            mtime_ns=int(raw["mtime_ns"]),
            quick_hash=str(raw.get("quick_hash", "")),
            has_audio=bool(raw.get("has_audio", False)),
            fps_num=int(raw.get("fps_num", 0)),
            fps_den=int(raw.get("fps_den", 1)),
            width=int(raw.get("width", 0)),
            height=int(raw.get("height", 0)),
        )


@dataclass(slots=True)
class Segment:
    source_file: str
    in_us: int
    out_us: int
    title: str
    purpose: str | None = None
    original_audio: str = "keep"
    speed_percent: int = 100
    first_line: str | None = None
    last_line: str | None = None
    id: str = field(default_factory=new_id)

    @property
    def source_duration_us(self) -> int:
        return self.out_us - self.in_us

    @property
    def duration_us(self) -> int:
        """当前成片中的时长；源区间始终由 ``source_duration_us`` 表示。"""

        return (
            self.source_duration_us * 100 + self.speed_percent // 2
        ) // self.speed_percent

    def source_offset_to_output_us(self, source_offset_us: int) -> int:
        return (
            int(source_offset_us) * 100 + self.speed_percent // 2
        ) // self.speed_percent

    def copy(self) -> "Segment":
        return Segment.from_dict(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_file": self.source_file,
            "in_us": self.in_us,
            "out_us": self.out_us,
            "title": self.title,
            "purpose": self.purpose,
            "original_audio": self.original_audio,
            "speed_percent": self.speed_percent,
            "first_line": self.first_line,
            "last_line": self.last_line,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Segment":
        return cls(
            id=str(raw.get("id") or new_id()),
            source_file=str(raw["source_file"]),
            in_us=int(raw["in_us"]),
            out_us=int(raw["out_us"]),
            title=str(raw.get("title") or "未命名片段"),
            purpose=raw.get("purpose"),
            original_audio=str(raw.get("original_audio") or "keep"),
            speed_percent=int(raw.get("speed_percent", 100)),
            first_line=raw.get("first_line"),
            last_line=raw.get("last_line"),
        )


@dataclass(slots=True)
class Cut:
    title: str
    segments: list[Segment]
    publishing: dict[str, Any] | None = None
    packaging: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "segments": [segment.to_dict() for segment in self.segments],
            "publishing": self.publishing,
            "packaging": self.packaging,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Cut":
        publishing = raw.get("publishing")
        if publishing is not None and not isinstance(publishing, dict):
            raise ManifestValidationError("工程中的 publishing 必须是对象或省略。")
        return cls(
            id=str(raw.get("id") or new_id()),
            title=str(raw.get("title") or "未命名切片"),
            segments=[Segment.from_dict(item) for item in raw.get("segments", [])],
            publishing=dict(publishing) if publishing is not None else None,
            packaging=deepcopy(raw.get("packaging") or {}),
        )


@dataclass(slots=True)
class ProjectDocument:
    """工程的唯一可编辑状态。

    history / redo 保存的是每次操作前后的整条片段序列。片段数通常很少，换来
    撤销、重做与持久化恢复的确定性；导出时只读取快照，不会修改本对象。
    """

    media_root: str
    drama: str
    original_manifest: dict[str, Any]
    sources: dict[str, SourceInfo]
    cuts: list[Cut]
    active_cut_id: str | None = None
    revision: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    redo_stack: list[dict[str, Any]] = field(default_factory=list)
    transcript_paths: list[str] = field(default_factory=list)
    transcript_bindings: dict[str, str] = field(default_factory=dict)
    transcript_overrides: dict[str, str] = field(default_factory=dict)
    planning_context: dict[str, Any] = field(default_factory=dict)
    resource_settings: dict[str, Any] = field(
        default_factory=lambda: {
            "mode": "standard",
            "cache_limit_bytes": 2 * 1024 * 1024 * 1024,
            "cpu_threads": 6,
        }
    )
    schema_version: int = 1

    def __post_init__(self) -> None:
        defaults = {
            "mode": "standard",
            "cache_limit_bytes": 2 * 1024 * 1024 * 1024,
            "cpu_threads": 6,
        }
        defaults.update(self.resource_settings)
        self.resource_settings = defaults
        if not self.active_cut_id and self.cuts:
            self.active_cut_id = self.cuts[0].id
        self.validate()

    @property
    def active_cut(self) -> Cut:
        if not self.active_cut_id:
            raise ManifestValidationError("工程没有可编辑的切片。")
        return self.get_cut(self.active_cut_id)

    def get_cut(self, cut_id: str) -> Cut:
        for cut in self.cuts:
            if cut.id == cut_id:
                return cut
        raise ManifestValidationError("找不到指定的切片。", detail=f"cut_id={cut_id}")

    def source_for(self, relative_path: str) -> SourceInfo:
        try:
            return self.sources[relative_path]
        except KeyError as exc:
            raise ManifestValidationError(
                "片段引用了未登记的素材文件。", detail=relative_path
            ) from exc

    def validate_segment(self, segment: Segment) -> None:
        if segment.original_audio not in {"keep", "mute"}:
            raise ManifestValidationError("原声策略只能是 keep 或 mute。")
        if not isinstance(segment.speed_percent, int) or isinstance(
            segment.speed_percent, bool
        ) or not 50 <= segment.speed_percent <= 200:
            raise ManifestValidationError("基础倍速只能在 50% 到 200% 之间。")
        if segment.in_us < 0:
            raise ManifestValidationError("片段入点不能为负数。", detail=segment.title)
        if segment.out_us <= segment.in_us:
            raise ManifestValidationError("片段出点必须大于入点。", detail=segment.title)
        source = self.source_for(segment.source_file)
        allowed_end = source.duration_us + source.frame_duration_us
        if segment.out_us > allowed_end:
            raise ManifestValidationError(
                "片段出点超出源素材时长。",
                detail=(
                    f"{segment.title}: 出点 {segment.out_us // 1000} ms，"
                    f"素材 {source.duration_us // 1000} ms"
                ),
            )

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ManifestValidationError("不支持的工程版本。")
        if not self.drama.strip():
            raise ManifestValidationError("工程缺少剧名。")
        if not self.sources:
            raise ManifestValidationError("工程没有素材映射。")
        if not self.cuts:
            raise ManifestValidationError("工程没有切片。")
        if self.resource_settings.get("mode") not in {"standard", "saver"}:
            raise ManifestValidationError("工程的资源模式无效。")
        if not isinstance(self.resource_settings.get("cpu_threads"), int):
            raise ManifestValidationError("工程的 CPU 线程设置无效。")
        if len(set(self.transcript_paths)) != len(self.transcript_paths):
            raise ManifestValidationError("工程登记了重复的台词文件。")
        if not isinstance(self.planning_context, dict) or any(
            not isinstance(key, str) or
            (type(value) is not bool if key == "include_narration" else not isinstance(value, str))
            for key, value in self.planning_context.items()
            if key not in {"form_options", "batch", "api_transport"}
        ):
            raise ManifestValidationError("工程的 AI 规划状态无效。")
        transport = self.planning_context.get("api_transport")
        if transport is not None and (not isinstance(transport, dict)
                or set(transport) != {"wait_minutes", "stream"}
                or type(transport.get("wait_minutes")) is not int
                or not 1 <= transport["wait_minutes"] <= 120
                or type(transport.get("stream")) is not bool):
            raise ManifestValidationError("工程的 API 接收设置无效。")
        batch = self.planning_context.get('batch')
        if batch is not None and (not isinstance(batch, dict) or set(batch) != {'name', 'directory', 'number'}
                or not isinstance(batch.get('name'), str) or not isinstance(batch.get('directory'), str)
                or type(batch.get('number')) is not int or batch['number'] < 1):
            raise ManifestValidationError('工程的剪辑批次状态无效。')
        options = self.planning_context.get("form_options", {})
        if not isinstance(options, dict) or any(
            key not in {"mode", "count", "narration_count", "duration", "direction", "extra", "template_text", "template_name", "template_kind"}
            or (type(value) is not int if key in {"mode", "count", "narration_count"} else not isinstance(value, str))
            for key, value in options.items()
        ):
            raise ManifestValidationError("工程的 AI 目标表单无效。")
        if options and (options.get("mode", 0) not in (0, 1) or not 1 <= options.get("count", 1) <= 100
                        or not 0 <= options.get("narration_count", 0) <= options.get("count", 1)):
            raise ManifestValidationError("工程的 AI 切片数量无效。")
        for path, source in self.transcript_bindings.items():
            if path not in self.transcript_paths:
                raise ManifestValidationError("台词绑定引用了未登记的台词文件。")
            self.source_for(source)
        known_ids: set[str] = set()
        for cut in self.cuts:
            if not cut.title.strip():
                raise ManifestValidationError("切片标题不能为空。")
            if cut.publishing is not None:
                self._validate_publishing(cut.publishing)
            if not cut.segments:
                raise ManifestValidationError("每个切片至少需要一个片段。", detail=cut.title)
            for segment in cut.segments:
                if segment.id in known_ids:
                    raise ManifestValidationError("工程内出现重复的片段标识。")
                known_ids.add(segment.id)
                self.validate_segment(segment)
            # 延迟导入避免数据模型与包装映射模块互相初始化；这里只校验可编辑
            # 工程数据，不读取用户图片／音频，也不会扫描素材目录。
            from .packaging import validate_packaging

            validate_packaging(
                cut.packaging,
                segments=cut.segments,
                source_files=self.sources.keys(),
            )

    def _validate_publishing(self, publishing: dict[str, Any]) -> None:
        kind = publishing.get("kind", "video")
        if kind not in {"video", "graphic"}:
            raise ManifestValidationError("工程的 publishing.kind 无效。")
        if not isinstance(publishing.get("title"), str) or not publishing["title"].strip():
            raise ManifestValidationError("工程的发布标题无效。")
        if not isinstance(publishing.get("body"), str) or not publishing["body"].strip():
            raise ManifestValidationError("工程的发布正文无效。")
        tags = publishing.get("tags")
        if (
            not isinstance(tags, list)
            or not 6 <= len(tags) <= 8
            or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
        ):
            raise ManifestValidationError("工程的发布标签无效。")
        expected = (
            "#漫剧榜单推荐"
            if kind == "graphic"
            else "#" + self.drama.strip().strip("《》").strip()
        )
        if tags[0].strip() != expected:
            raise ManifestValidationError("工程的发布首标签不符合当前规则。")

    def total_duration_us(self, cut_id: str | None = None) -> int:
        from .timeline import total_duration_us

        cut = self.get_cut(cut_id or self.active_cut.id)
        return total_duration_us(cut)

    def _segments_snapshot(self, cut_id: str) -> list[dict[str, Any]]:
        return [segment.to_dict() for segment in self.get_cut(cut_id).segments]

    def _set_segments(
        self, cut_id: str, items: list[dict[str, Any]],
        *, packaging: dict[str, Any] | None = None,
    ) -> None:
        cut = self.get_cut(cut_id)
        replacement = [Segment.from_dict(item) for item in items]
        if not replacement:
            raise ManifestValidationError("不能删除切片中的最后一个片段。")
        for segment in replacement:
            self.validate_segment(segment)
        if len({segment.id for segment in replacement}) != len(replacement):
            raise ManifestValidationError("片段标识重复，未应用改动。")
        if packaging is not None:
            from .packaging import validate_packaging
            validate_packaging(packaging, segments=replacement, source_files=self.sources.keys())
        # Validate the complete replacement before mutating either half.
        cut.segments = replacement
        if packaging is not None:
            cut.packaging = deepcopy(packaging)

    def replace_segments(
        self, cut_id: str, replacement: list[Segment], description: str,
        *, split_children: dict[str, list[str]] | None = None,
    ) -> bool:
        before = self._segments_snapshot(cut_id)
        after = [segment.to_dict() for segment in replacement]
        if before == after:
            return False
        from .packaging import packaging_after_segment_edit
        cut = self.get_cut(cut_id)
        before_packaging = deepcopy(cut.packaging)
        after_packaging = packaging_after_segment_edit(
            cut.packaging, cut.segments, replacement, split_children=split_children,
        )
        self._set_segments(cut_id, after, packaging=after_packaging)
        self.history.append(
            {
                "kind": "set_segments",
                "cut_id": cut_id,
                "before": before,
                "after": after,
                "before_packaging": before_packaging,
                "after_packaging": deepcopy(after_packaging),
                "description": description,
            }
        )
        self.redo_stack.clear()
        self.revision += 1
        return True

    def adjust_segment_end(self, cut_id: str, index: int, delta_us: int) -> None:
        cut = self.get_cut(cut_id)
        if not 0 <= index < len(cut.segments):
            raise ManifestValidationError("要调整的片段不存在。")
        replacement = [segment.copy() for segment in cut.segments]
        replacement[index].out_us += int(delta_us)
        self.replace_segments(cut_id, replacement, "调整片段出点")

    def adjust_segment_start(self, cut_id: str, index: int, delta_us: int) -> None:
        cut = self.get_cut(cut_id)
        if not 0 <= index < len(cut.segments):
            raise ManifestValidationError("要调整的片段不存在。")
        replacement = [segment.copy() for segment in cut.segments]
        replacement[index].in_us += int(delta_us)
        self.replace_segments(cut_id, replacement, "调整片段入点")

    def split_segment(self, cut_id: str, index: int, source_position_us: int) -> None:
        """在原视频时间处分割，保留全部画面并形成可插卡的接缝。"""

        cut = self.get_cut(cut_id)
        if not 0 <= index < len(cut.segments):
            raise ManifestValidationError("要分割的片段不存在。")
        original = cut.segments[index]
        if not original.in_us < source_position_us < original.out_us:
            raise ManifestValidationError("分割时间必须位于选中片段的入点和出点之间。")
        replacement = [segment.copy() for segment in cut.segments]
        left = original.copy()
        right = original.copy()
        left.out_us = source_position_us
        right.in_us = source_position_us
        right.id = new_id()
        right.title = f"{original.title}（后段）"
        replacement[index:index + 1] = [left, right]
        self.replace_segments(cut_id, replacement, "按原视频时间分割片段",
                              split_children={original.id: [left.id, right.id]})

    def delete_segment(self, cut_id: str, index: int) -> None:
        cut = self.get_cut(cut_id)
        if not 0 <= index < len(cut.segments):
            raise ManifestValidationError("要删除的片段不存在。")
        replacement = [segment.copy() for segment in cut.segments]
        del replacement[index]
        self.replace_segments(cut_id, replacement, "删除片段")

    def reorder_segments(self, cut_id: str, segment_ids: list[str]) -> None:
        cut = self.get_cut(cut_id)
        current = {segment.id: segment for segment in cut.segments}
        if set(segment_ids) != set(current) or len(segment_ids) != len(current):
            raise ManifestValidationError("拖拽后的片段顺序不完整，未应用改动。")
        replacement = [current[segment_id].copy() for segment_id in segment_ids]
        self.replace_segments(cut_id, replacement, "拖拽重排片段")

    def set_segment_speed(self, cut_id: str, index: int, speed_percent: int) -> None:
        cut = self.get_cut(cut_id)
        if not 0 <= index < len(cut.segments):
            raise ManifestValidationError("要调整倍速的片段不存在。")
        replacement = [segment.copy() for segment in cut.segments]
        replacement[index].speed_percent = int(speed_percent)
        self.replace_segments(cut_id, replacement, "调整片段倍速")

    def set_segment_original_audio(
        self, cut_id: str, index: int, original_audio: str
    ) -> None:
        cut = self.get_cut(cut_id)
        if not 0 <= index < len(cut.segments):
            raise ManifestValidationError("要调整原声的片段不存在。")
        replacement = [segment.copy() for segment in cut.segments]
        replacement[index].original_audio = str(original_audio)
        self.replace_segments(cut_id, replacement, "切换片段原声")

    def _set_packaging(self, cut_id: str, packaging: dict[str, Any]) -> None:
        from .packaging import freeze_audio_anchors, normalize_packaging, validate_packaging

        cut = self.get_cut(cut_id)
        normalized = normalize_packaging(packaging)
        validate_packaging(
            normalized,
            segments=cut.segments,
            source_files=self.sources.keys(),
        )
        cut.packaging = freeze_audio_anchors(normalized, cut.segments)

    def replace_packaging(
        self, cut_id: str, replacement: dict[str, Any], description: str
    ) -> bool:
        cut = self.get_cut(cut_id)
        before = deepcopy(cut.packaging)
        after = deepcopy(replacement)
        if before == after:
            return False
        self._set_packaging(cut_id, after)
        self.history.append(
            {
                "kind": "set_packaging",
                "cut_id": cut_id,
                "before": before,
                "after": deepcopy(self.get_cut(cut_id).packaging),
                "description": description,
            }
        )
        self.redo_stack.clear()
        self.revision += 1
        return True

    def set_transcript_override(self, cue_key: str, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            raise ManifestValidationError("校正后的台词不能为空。")
        before = self.transcript_overrides.get(cue_key)
        if before == normalized:
            return
        record = {
            "kind": "set_transcript_override",
            "cue_key": cue_key,
            "before": before,
            "after": normalized,
            "description": "校正台词文本",
        }
        self._apply_record(record, forward=True)
        self.history.append(record)
        self.redo_stack.clear()
        self.revision += 1

    def set_transcript_files(
        self, paths: list[str], *, bindings: dict[str, str] | None = None
    ) -> None:
        """登记只读台词文件及单集 SRT 对应的素材。

        文件内容不复制进工程；这样既复用已有转写结果，也不会把转写模型或整剧
        数据嵌入工程。登记本身会改变候选输入，因此作为可撤销的修订写入历史。
        """

        normalized_paths = list(dict.fromkeys(str(item) for item in paths if str(item)))
        normalized_bindings = {
            str(path): str(source).replace("\\", "/")
            for path, source in (bindings or {}).items()
            if str(path) in normalized_paths
        }
        for source in normalized_bindings.values():
            self.source_for(source)
        before = {
            "paths": list(self.transcript_paths),
            "bindings": dict(self.transcript_bindings),
        }
        after = {"paths": normalized_paths, "bindings": normalized_bindings}
        if before == after:
            return
        record = {
            "kind": "set_transcript_files",
            "before": before,
            "after": after,
            "description": "更新台词文件",
        }
        self._apply_record(record, forward=True)
        self.history.append(record)
        self.redo_stack.clear()
        self.revision += 1

    def set_resource_mode(self, mode: str) -> None:
        if mode not in {"standard", "saver"}:
            raise ManifestValidationError("资源模式只能是 standard 或 saver。")
        before = dict(self.resource_settings)
        after = dict(before)
        after["mode"] = mode
        after["cpu_threads"] = 2 if mode == "saver" else 6
        if before == after:
            return
        record = {
            "kind": "set_resource_settings",
            "before": before,
            "after": after,
            "description": "切换资源模式",
        }
        self._apply_record(record, forward=True)
        self.history.append(record)
        self.redo_stack.clear()
        self.revision += 1

    def _apply_record(self, record: dict[str, Any], *, forward: bool) -> None:
        kind = record.get("kind")
        if kind == "set_segments":
            self._set_segments(
                str(record["cut_id"]),
                list(record["after"] if forward else record["before"]),
                packaging=record.get("after_packaging" if forward else "before_packaging"),
            )
            return
        if kind == "set_transcript_override":
            value = record["after"] if forward else record["before"]
            key = str(record["cue_key"])
            if value is None:
                self.transcript_overrides.pop(key, None)
            else:
                self.transcript_overrides[key] = str(value)
            return
        if kind == "set_transcript_files":
            state = record["after"] if forward else record["before"]
            self.transcript_paths = [str(item) for item in state["paths"]]
            self.transcript_bindings = {
                str(path): str(source)
                for path, source in dict(state["bindings"]).items()
            }
            return
        if kind == "set_resource_settings":
            self.resource_settings = {
                str(key): value
                for key, value in dict(
                    record["after"] if forward else record["before"]
                ).items()
            }
            return
        if kind == "set_packaging":
            self._set_packaging(
                str(record["cut_id"]),
                deepcopy(record["after"] if forward else record["before"]),
            )
            return
        raise ManifestValidationError("工程包含不支持的撤销记录。", detail=str(kind))

    def undo(self) -> bool:
        if not self.history:
            return False
        record = self.history.pop()
        self._apply_record(record, forward=False)
        self.redo_stack.append(record)
        self.revision += 1
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        record = self.redo_stack.pop()
        self._apply_record(record, forward=True)
        self.history.append(record)
        self.revision += 1
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "media_root": self.media_root,
            "drama": self.drama,
            "original_manifest": self.original_manifest,
            "sources": {
                key: value.to_dict() for key, value in sorted(self.sources.items())
            },
            "cuts": [cut.to_dict() for cut in self.cuts],
            "active_cut_id": self.active_cut_id,
            "revision": self.revision,
            "history": self.history,
            "redo_stack": self.redo_stack,
            "transcript_paths": self.transcript_paths,
            "transcript_bindings": self.transcript_bindings,
            "transcript_overrides": self.transcript_overrides,
            "planning_context": dict(self.planning_context),
            "resource_settings": self.resource_settings,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProjectDocument":
        return cls(
            schema_version=int(raw.get("schema_version", 0)),
            media_root=str(raw["media_root"]),
            drama=str(raw["drama"]),
            original_manifest=dict(raw.get("original_manifest") or {}),
            sources={
                str(key): SourceInfo.from_dict(value)
                for key, value in dict(raw.get("sources") or {}).items()
            },
            cuts=[Cut.from_dict(item) for item in raw.get("cuts") or []],
            active_cut_id=raw.get("active_cut_id"),
            revision=int(raw.get("revision", 0)),
            history=list(raw.get("history") or []),
            redo_stack=list(raw.get("redo_stack") or []),
            transcript_paths=[str(item) for item in raw.get("transcript_paths") or []],
            transcript_bindings={
                str(path): str(source)
                for path, source in dict(raw.get("transcript_bindings") or {}).items()
            },
            transcript_overrides={
                str(key): str(value)
                for key, value in dict(raw.get("transcript_overrides") or {}).items()
            },
            resource_settings=dict(raw.get("resource_settings") or {}),
            planning_context=dict(raw.get("planning_context") or {}),
        )
