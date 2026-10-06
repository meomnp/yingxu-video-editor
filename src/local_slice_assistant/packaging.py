"""第四阶段的可编辑包装数据与时间轴映射。

这里保存的是工程可回改的事件，不是已经烧进视频的一层像素。字幕事件始终以
“源文件 + 源时间”锚定；同一集被重复使用或重排时，会在每个相交的片段上重新
生成显示实例。贴纸遮挡和新字幕共用源事件，但各层时间、开关可独立校准。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import uuid4

from .errors import ManifestValidationError


PACKAGING_VERSION = 2
_SUBTITLE_KINDS = {"srt", "transcript_candidate", "manual", "screen_text_detection"}
_AUDIO_KINDS = {"voiceover", "music", "effect"}
_CARD_KINDS = {"text", "image"}
_CARD_POSITIONS = {"before", "after", "end"}


def new_packaging_id() -> str:
    return uuid4().hex


def default_packaging() -> dict[str, Any]:
    """返回每个 cut 独享的默认包装状态。"""

    return {
        "version": PACKAGING_VERSION,
        "caption_style": {
            "sticker": {
                "enabled": True,
                # 常见竖屏短剧的硬字幕在下方中部。这是一个有余量的窄区，
                # 不是整块下三分之一的白板；用户仍可在“字幕与遮挡”中校准。
                "x": 0.20,
                "y": 0.685,
                "width": 0.60,
                "height": 0.075,
                "color": "white",
                # 默认使用通用半透明白色柔边，不要求用户准备任何风格贴纸。
                # 中心层近乎不透以压住硬字幕，边缘层保持轻盈。
                "opacity": 0.96,
                "preset": "warm_white_soft",
                "image_path": None,
                "fit_mode": "three_slice",
                "corner_radius": 0.02,
                "feather": 0.012,
            },
            "new_text": {
                "enabled": True,
                "font_path": None,
                "font_size": 36,
                "color": "black",
                "alignment": "center",
                "margin_y": 0.022,
            },
        },
        "caption_detection": {
            # 与遮挡区保持一致，先在真正可能出现字幕的位置做形态检测。
            "roi": {"x": 0.20, "y": 0.665, "width": 0.60, "height": 0.115},
        },
        "subtitle_events": [],
        "subtitle_instance_overrides": {},
        "title_cards": [],
        "audio_items": [],
    }


def normalize_packaging(raw: dict[str, Any] | None) -> dict[str, Any]:
    """补齐旧工程没有的字段；不悄悄猜测用户的事件内容。"""

    if raw is None:
        return default_packaging()
    if not isinstance(raw, dict):
        raise ManifestValidationError("包装工程数据必须是对象。")
    result = default_packaging()
    result.update(deepcopy(raw))
    style = raw.get("caption_style")
    if style is None:
        return result
    if not isinstance(style, dict):
        raise ManifestValidationError("字幕样式必须是对象。")
    normalized_style = deepcopy(default_packaging()["caption_style"])
    normalized_style.update(deepcopy(style))
    for name in ("sticker", "new_text"):
        value = style.get(name)
        if value is not None:
            if not isinstance(value, dict):
                raise ManifestValidationError(f"字幕样式 {name} 必须是对象。")
            merged = deepcopy(default_packaging()["caption_style"][name])
            merged.update(deepcopy(value))
            normalized_style[name] = merged
    result["caption_style"] = normalized_style
    detection = raw.get("caption_detection")
    if detection is not None:
        if not isinstance(detection, dict) or not isinstance(detection.get("roi", {}), dict):
            raise ManifestValidationError("字幕检测区域必须是对象。")
        normalized_detection = deepcopy(default_packaging()["caption_detection"])
        normalized_detection["roi"].update(deepcopy(detection["roi"]))
        result["caption_detection"] = normalized_detection
    # v1 事件共用一段时间。保留原字段，按旧含义补齐两层的独立区间，
    # 因而旧工程重新打开时不会改变可见结果。
    for event in result.get("subtitle_events", []):
        if isinstance(event, dict):
            for layer in ("sticker", "new_text"):
                event.setdefault(f"{layer}_source_in_us", event.get("source_in_us"))
                event.setdefault(f"{layer}_source_out_us", event.get("source_out_us"))
    result["version"] = PACKAGING_VERSION
    return result


def subtitle_instance_key(event_id: str, segment_id: str) -> str:
    return f"{event_id}@{segment_id}"


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_bool(raw: dict[str, Any], key: str, label: str) -> None:
    if key in raw and not isinstance(raw[key], bool):
        raise ManifestValidationError(f"{label}必须是开或关。")


def _require_int(raw: dict[str, Any], key: str, label: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManifestValidationError(f"{label}必须是整数时间。")
    return value


def _validate_caption_style(raw: dict[str, Any]) -> None:
    if not isinstance(raw, dict):
        raise ManifestValidationError("字幕样式必须是对象。")
    sticker = raw.get("sticker")
    text = raw.get("new_text")
    if not isinstance(sticker, dict) or not isinstance(text, dict):
        raise ManifestValidationError("字幕遮挡和新字幕样式必须完整。")
    _require_bool(sticker, "enabled", "遮挡贴纸开关")
    _require_bool(text, "enabled", "新字幕开关")
    for key in ("x", "y", "width", "height", "opacity"):
        value = sticker.get(key)
        if not _is_number(value):
            raise ManifestValidationError(f"遮挡贴纸的 {key} 必须是数字。")
    if not 0 <= float(sticker["x"]) <= 1 or not 0 <= float(sticker["y"]) <= 1:
        raise ManifestValidationError("遮挡贴纸位置必须在画布内。")
    if not 0 < float(sticker["width"]) <= 1 or not 0 < float(sticker["height"]) <= 1:
        raise ManifestValidationError("遮挡贴纸宽高必须大于零且不超过画布。")
    if float(sticker["x"]) + float(sticker["width"]) > 1.001 or float(sticker["y"]) + float(sticker["height"]) > 1.001:
        raise ManifestValidationError("遮挡贴纸不能超出画布。")
    if not 0 <= float(sticker["opacity"]) <= 1:
        raise ManifestValidationError("遮挡贴纸透明度必须在 0 到 1 之间。")
    if not isinstance(sticker.get("color"), str) or not sticker["color"].strip():
        raise ManifestValidationError("遮挡贴纸颜色不能为空。")
    if sticker.get("preset") not in {"pure_white", "warm_white_soft", "dark_gray"}:
        raise ManifestValidationError("遮挡贴纸样式无效。")
    if sticker.get("image_path") is not None and not isinstance(sticker.get("image_path"), str):
        raise ManifestValidationError("遮挡贴纸图片路径必须是文字或留空。")
    if sticker.get("fit_mode") not in {"three_slice", "stretch"}:
        raise ManifestValidationError("贴纸适配方式无效。")
    for key in ("corner_radius", "feather"):
        value = sticker.get(key)
        if not _is_number(value) or not 0 <= float(value) <= 0.25:
            raise ManifestValidationError(f"遮挡贴纸的 {key} 必须在 0 到 0.25 之间。")
    if text.get("font_path") is not None and not isinstance(text.get("font_path"), str):
        raise ManifestValidationError("字幕字体路径必须是文字或留空。")
    size = text.get("font_size")
    if not isinstance(size, int) or isinstance(size, bool) or not 8 <= size <= 240:
        raise ManifestValidationError("字幕字号必须在 8 到 240 之间。")
    if not isinstance(text.get("color"), str) or not text["color"].strip():
        raise ManifestValidationError("新字幕颜色不能为空。")
    if text.get("alignment") not in {"left", "center", "right"}:
        raise ManifestValidationError("新字幕对齐方式无效。")
    margin = text.get("margin_y")
    if not _is_number(margin) or not 0 <= float(margin) <= 0.5:
        raise ManifestValidationError("新字幕垂直边距必须在画布内。")


def _validate_detection_region(raw: dict[str, Any]) -> None:
    roi = raw.get("roi") if isinstance(raw, dict) else None
    if not isinstance(roi, dict):
        raise ManifestValidationError("字幕检测区域必须完整。")
    for key in ("x", "y", "width", "height"):
        if not _is_number(roi.get(key)):
            raise ManifestValidationError(f"字幕检测区域的 {key} 必须是数字。")
    if not 0 <= float(roi["x"]) <= 1 or not 0 <= float(roi["y"]) <= 1:
        raise ManifestValidationError("字幕检测区域位置必须在画布内。")
    if not 0 < float(roi["width"]) <= 1 or not 0 < float(roi["height"]) <= 1:
        raise ManifestValidationError("字幕检测区域宽高必须有效。")
    if float(roi["x"]) + float(roi["width"]) > 1.001 or float(roi["y"]) + float(roi["height"]) > 1.001:
        raise ManifestValidationError("字幕检测区域不能超出画布。")


def _validate_sticker_box(raw: object) -> None:
    """逐条画面检测得到的遮挡框；缺省时回退到全局样式框。"""

    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ManifestValidationError("逐条字幕贴纸框必须是对象。")
    for key in ("x", "y", "width", "height"):
        if not _is_number(raw.get(key)):
            raise ManifestValidationError(f"逐条字幕贴纸框的 {key} 必须是数字。")
    if not 0 <= float(raw["x"]) <= 1 or not 0 <= float(raw["y"]) <= 1:
        raise ManifestValidationError("逐条字幕贴纸框位置必须在画布内。")
    if not 0 < float(raw["width"]) <= 1 or not 0 < float(raw["height"]) <= 1:
        raise ManifestValidationError("逐条字幕贴纸框宽高必须有效。")
    if float(raw["x"]) + float(raw["width"]) > 1.001 or float(raw["y"]) + float(raw["height"]) > 1.001:
        raise ManifestValidationError("逐条字幕贴纸框不能超出画布。")


def _validate_attachment_anchor(item: dict[str, Any], segment_ids: set[str], source_files: set[str]) -> None:
    anchor = item.get("anchor_segment_id")
    detached = item.get("detached_segment")
    if detached is not None:
        if not isinstance(detached, dict) or detached.get("id") != anchor or detached.get("source_file") not in source_files:
            raise ManifestValidationError("待安排素材的原承载片段记录无效。")
        start = _require_int(detached, "in_us", "原承载片段起点")
        end = _require_int(detached, "out_us", "原承载片段终点")
        if not 0 <= start < end:
            raise ManifestValidationError("待安排素材的原承载片段区间无效。")
    if anchor not in segment_ids and detached is None:
        raise ManifestValidationError("包装锚点片段不存在，且没有待重新安排记录。")


def validate_packaging(
    raw: dict[str, Any] | None,
    *,
    segments: Iterable[Any],
    source_files: Iterable[str],
) -> None:
    """验证包装引用，不访问或扫描用户提供的媒体文件。"""

    packaging = normalize_packaging(raw)
    if packaging.get("version") != PACKAGING_VERSION:
        raise ManifestValidationError("不支持的包装工程版本。")
    _validate_caption_style(packaging["caption_style"])
    _validate_detection_region(packaging["caption_detection"])
    source_set = set(source_files)
    segment_ids = {str(segment.id) for segment in segments}
    events = packaging.get("subtitle_events")
    if not isinstance(events, list):
        raise ManifestValidationError("字幕事件必须是列表。")
    event_ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            raise ManifestValidationError("字幕事件必须是对象。")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id or event_id in event_ids:
            raise ManifestValidationError("字幕事件标识缺失或重复。")
        event_ids.add(event_id)
        source_file = event.get("source_file")
        if not isinstance(source_file, str) or source_file not in source_set:
            raise ManifestValidationError("字幕事件没有关联到已登记的源素材。")
        start = _require_int(event, "source_in_us", "字幕起点")
        end = _require_int(event, "source_out_us", "字幕终点")
        if end <= start:
            raise ManifestValidationError("字幕终点必须晚于起点。")
        for layer in ("sticker", "new_text"):
            layer_start = _require_int(event, f"{layer}_source_in_us", f"{layer}起点")
            layer_end = _require_int(event, f"{layer}_source_out_us", f"{layer}终点")
            if layer_end <= layer_start:
                raise ManifestValidationError(f"{layer}终点必须晚于起点。")
        if not isinstance(event.get("text"), str):
            raise ManifestValidationError("字幕文本必须是文字。")
        if event.get("source_kind", "manual") not in _SUBTITLE_KINDS:
            raise ManifestValidationError("字幕事件来源类型无效。")
        _require_bool(event, "sticker_enabled", "字幕遮挡开关")
        _require_bool(event, "new_text_enabled", "新字幕开关")
        _validate_sticker_box(event.get("sticker_box"))
    overrides = packaging.get("subtitle_instance_overrides")
    if not isinstance(overrides, dict):
        raise ManifestValidationError("字幕逐条校准必须是对象。")
    for key, value in overrides.items():
        if not isinstance(key, str) or "@" not in key or not isinstance(value, dict):
            raise ManifestValidationError("字幕逐条校准数据无效。")
        event_id, segment_id = key.split("@", maxsplit=1)
        if event_id not in event_ids or segment_id not in segment_ids:
            raise ManifestValidationError("字幕逐条校准引用了不存在的字幕或片段。")
        for offset_key in ("start_offset_us", "end_offset_us"):
            if offset_key in value:
                amount = _require_int(value, offset_key, "字幕时间偏移")
                if abs(amount) > 30_000_000:
                    raise ManifestValidationError("单条字幕时间偏移不能超过 30 秒。")
        for layer in ("sticker", "new_text"):
            for suffix in ("start_offset_us", "end_offset_us"):
                offset_key = f"{layer}_{suffix}"
                if offset_key in value:
                    amount = _require_int(value, offset_key, "字幕层时间偏移")
                    if abs(amount) > 30_000_000:
                        raise ManifestValidationError("单条字幕层时间偏移不能超过 30 秒。")
        if "text" in value and (not isinstance(value["text"], str) or not value["text"].strip()):
            raise ManifestValidationError("单条校准字幕文本不能为空。")
        _validate_sticker_box(value.get("sticker_box"))
        bounds = value.get("timing_source_bounds")
        if bounds is not None and (
            not isinstance(bounds, (list, tuple)) or len(bounds) != 2
            or any(not isinstance(part, int) or isinstance(part, bool) for part in bounds)
            or not 0 <= bounds[0] < bounds[1]
        ):
            raise ManifestValidationError("字幕校准的原承载区间必须是有效的整数源时间。")
        _require_bool(value, "sticker_enabled", "逐条遮挡开关")
        _require_bool(value, "new_text_enabled", "逐条新字幕开关")
    cards = packaging.get("title_cards")
    if not isinstance(cards, list):
        raise ManifestValidationError("字卡列表必须是列表。")
    card_ids: set[str] = set()
    for card in cards:
        if not isinstance(card, dict):
            raise ManifestValidationError("字卡必须是对象。")
        card_id = card.get("id")
        if not isinstance(card_id, str) or not card_id or card_id in card_ids:
            raise ManifestValidationError("字卡标识缺失或重复。")
        card_ids.add(card_id)
        if card.get("kind", "text") not in _CARD_KINDS:
            raise ManifestValidationError("字卡类型无效。")
        anchor = card.get("anchor_segment_id")
        if anchor is not None:
            _validate_attachment_anchor(card, segment_ids, source_set)
        _require_bool(card, "enabled", "字卡开关")
        if card.get("position", "after") not in _CARD_POSITIONS:
            raise ManifestValidationError("字卡位置无效。")
        duration = _require_int(card, "duration_us", "字卡时长")
        if not 1 <= duration <= 60_000_000:
            raise ManifestValidationError("字卡时长必须在 0 到 60 秒之间。")
        if not isinstance(card.get("text", ""), str):
            raise ManifestValidationError("字卡文本必须是文字。")
        if card.get("kind", "text") == "image":
            if not isinstance(card.get("image_path"), str) or not card["image_path"].strip():
                raise ManifestValidationError("图片字卡需要用户选择图片文件。")
    audio_items = packaging.get("audio_items")
    if not isinstance(audio_items, list):
        raise ManifestValidationError("音频包装列表必须是列表。")
    audio_ids: set[str] = set()
    for item in audio_items:
        if not isinstance(item, dict):
            raise ManifestValidationError("音频包装项必须是对象。")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in audio_ids:
            raise ManifestValidationError("音频包装标识缺失或重复。")
        audio_ids.add(item_id)
        if item.get("kind") not in _AUDIO_KINDS:
            raise ManifestValidationError("音频包装类型无效。")
        if not isinstance(item.get("file_path"), str) or not item["file_path"].strip():
            raise ManifestValidationError("音频包装需要用户选择文件。")
        _validate_attachment_anchor(item, segment_ids, source_set)
        source_offset = _require_int(item, "source_offset_us", "音频锚点")
        if source_offset < 0:
            raise ManifestValidationError("音频锚点不能早于承载片段。")
        if "anchor_source_us" in item and _require_int(item, "anchor_source_us", "声音源时间锚点") < 0:
            raise ManifestValidationError("声音源时间锚点不能为负数。")
        source_in = _require_int(item, "source_in_us", "音频源起点")
        duration = _require_int(item, "duration_us", "音频时长")
        if source_in < 0 or duration <= 0:
            raise ManifestValidationError("音频源起点和时长必须有效。")
        _require_bool(item, "enabled", "音频包装开关")
        _require_bool(item, "mute_original", "原声静音开关")
        if item.get("kind") == "voiceover":
            script = item.get("script")
            if not isinstance(script, str) or not script.strip():
                raise ManifestValidationError("解说配音必须在工程中保存完整解说词。")
        volume = item.get("volume", 1.0)
        if not _is_number(volume) or not 0 <= float(volume) <= 4:
            raise ManifestValidationError("音频音量必须在 0 到 4 之间。")


def freeze_audio_anchors(raw: dict[str, Any], segments: Iterable[Any]) -> dict[str, Any]:
    """Upgrade legacy relative anchors before changing a segment's source in-point.

    source_in_us is the WAV trim position, NOT the video's source timestamp.
    Keep source_offset_us for older projects; new mapping uses anchor_source_us.
    """
    result = normalize_packaging(raw)
    by_id = {segment.id: segment for segment in segments}
    for item in result["audio_items"]:
        segment = by_id.get(item["anchor_segment_id"])
        if segment is not None and "anchor_source_us" not in item:
            item["anchor_source_us"] = segment.in_us + int(item["source_offset_us"])
    return result


def packaging_after_segment_edit(
    raw: dict[str, Any], before: list[Any], after: list[Any],
    *, split_children: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Reconcile attached layers in the SAME undoable edit as their carrier.

    A deleted carrier leaves attachments pending, never discards files or work.
    A split has explicit lineage: never guess using overlapping/repeated sources.
    """
    result = freeze_audio_anchors(raw, before)
    by_id = {segment.id: segment for segment in after}
    before_by_id = {segment.id: segment for segment in before}
    splits = split_children or {}
    def retain_detached(item: dict[str, Any]) -> None:
        identity = item.get("anchor_segment_id")
        if identity not in by_id and identity in before_by_id:
            parent = before_by_id[identity]
            item["detached_segment"] = dict(id=parent.id, source_file=parent.source_file,
                                            in_us=parent.in_us, out_us=parent.out_us)

    for item in result["audio_items"]:
        identity = item["anchor_segment_id"]
        if identity in splits:
            source_time = item["anchor_source_us"]
            children = [by_id[key] for key in splits[identity]]
            child = next((part for part in children if part.in_us <= source_time < part.out_us), None)
            if child is not None:
                item["anchor_segment_id"] = child.id
                item["source_offset_us"] = source_time - child.in_us
        retain_detached(item)
    for card in result["title_cards"]:
        identity = card.get("anchor_segment_id")
        position = card.get("position", "after")
        if identity in splits:
            card["anchor_segment_id"] = splits[identity][-1 if position == "after" else 0]
        if position == "end":
            card["anchor_segment_id"] = None
            card.pop("detached_segment", None)
        else:
            retain_detached(card)
    # Calibration belongs to a particular use of a source, not every repeat.
    # Keep its original clipping domain: applying an offset *after* clipping to
    # each new child would shift the split boundary or lose a displaced layer.
    overrides = {}
    for key, value in result["subtitle_instance_overrides"].items():
        event_id, identity = key.split("@", 1)
        if identity in splits:
            parent = before_by_id[identity]
            inherited = deepcopy(value)
            inherited.setdefault("timing_source_bounds", [parent.in_us, parent.out_us])
            for child_id in splits[identity]:
                overrides[subtitle_instance_key(event_id, child_id)] = deepcopy(inherited)
        elif identity in by_id:
            overrides[key] = value
    result["subtitle_instance_overrides"] = overrides
    return result


def title_cards_for_cut(cut: Any) -> list[dict[str, Any]]:
    return [deepcopy(item) for item in normalize_packaging(cut.packaging)["title_cards"] if item.get("enabled", True)]


def reassign_attachment(cut: Any, collection: str, item_id: str, segment_id: str, *, source_offset_us: int = 0) -> dict[str, Any]:
    """Explicit, undoable reattachment; never choose new dialogue or truncate audio."""
    if collection not in {"audio_items", "title_cards"}:
        raise ManifestValidationError("待安排素材类型无效。")
    result = normalize_packaging(cut.packaging)
    item = next((part for part in result[collection] if part["id"] == item_id), None)
    segment = next((part for part in cut.segments if part.id == segment_id), None)
    if item is None or segment is None:
        raise ManifestValidationError("待安排素材或目标片段不存在。")
    if collection == "audio_items":
        if not isinstance(source_offset_us, int) or isinstance(source_offset_us, bool) or not 0 <= source_offset_us < segment.out_us - segment.in_us:
            raise ManifestValidationError("声音锚点必须位于目标片段内。")
        if _source_offset_to_output_us(segment, source_offset_us) + int(item["duration_us"]) > segment.duration_us:
            raise ManifestValidationError("声音比目标片段剩余时长更长，请选择更长的片段或先裁短声音；未截断配音。")
        item["source_offset_us"] = source_offset_us
        item["anchor_source_us"] = segment.in_us + source_offset_us
    item["anchor_segment_id"] = segment.id
    item.pop("detached_segment", None)
    return result


def make_subtitle_event(
    *,
    source_file: str,
    source_in_us: int,
    source_out_us: int,
    text: str,
    source_kind: str,
    sticker_source_in_us: int | None = None,
    sticker_source_out_us: int | None = None,
    new_text_source_in_us: int | None = None,
    new_text_source_out_us: int | None = None,
    sticker_box: dict[str, float] | None = None,
) -> dict[str, Any]:
    event = {
        "id": new_packaging_id(),
        "source_file": source_file,
        "source_in_us": int(source_in_us),
        "source_out_us": int(source_out_us),
        "text": str(text),
        "source_kind": source_kind,
        "sticker_source_in_us": int(sticker_source_in_us if sticker_source_in_us is not None else source_in_us),
        "sticker_source_out_us": int(sticker_source_out_us if sticker_source_out_us is not None else source_out_us),
        "new_text_source_in_us": int(new_text_source_in_us if new_text_source_in_us is not None else source_in_us),
        "new_text_source_out_us": int(new_text_source_out_us if new_text_source_out_us is not None else source_out_us),
        "sticker_enabled": True,
        "new_text_enabled": True,
    }
    if sticker_box is not None:
        event["sticker_box"] = {
            key: float(sticker_box[key]) for key in ("x", "y", "width", "height")
        }
    return event


def events_from_cues(cues: Iterable[Any]) -> list[dict[str, Any]]:
    """把已有 SRT／转写 cue 变成待校准候选，而不声称其等同画面硬字幕。"""

    return [
        make_subtitle_event(
            source_file=str(cue.source_file),
            source_in_us=int(cue.start_us),
            source_out_us=int(cue.end_us),
            text=str(cue.text),
            source_kind=str(getattr(cue, "source_kind", "transcript_candidate")),
        )
        for cue in cues
        if int(cue.end_us) > int(cue.start_us)
    ]


def append_missing_events(
    packaging: dict[str, Any], events: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """按源文件、时间和文本去重地加入候选；不覆盖用户已有逐条校准。"""

    updated = normalize_packaging(packaging)
    known = {
        (
            item.get("source_file"),
            item.get("source_in_us"),
            item.get("source_out_us"),
            item.get("text"),
        )
        for item in updated["subtitle_events"]
    }
    for event in events:
        identity = (
            event.get("source_file"),
            event.get("source_in_us"),
            event.get("source_out_us"),
            event.get("text"),
        )
        if identity not in known:
            updated["subtitle_events"].append(deepcopy(event))
            known.add(identity)
    return updated


@dataclass(frozen=True, slots=True)
class MappedSubtitleEvent:
    event_id: str
    instance_key: str
    segment_id: str
    source_file: str
    source_in_us: int
    source_out_us: int
    output_in_us: int
    output_out_us: int
    sticker_output_in_us: int
    sticker_output_out_us: int
    new_text_output_in_us: int
    new_text_output_out_us: int
    text: str
    source_kind: str
    sticker_enabled: bool
    new_text_enabled: bool
    sticker_box: dict[str, float] | None


@dataclass(frozen=True, slots=True)
class MappedAudioItem:
    item_id: str
    kind: str
    file_path: str
    anchor_segment_id: str
    output_in_us: int | None
    enabled: bool
    mute_original: bool
    volume: float
    needs_rearrangement: bool
    reason: str | None = None


def _source_offset_to_output_us(segment: Any, source_offset_us: int) -> int:
    converter = getattr(segment, "source_offset_to_output_us", None)
    if callable(converter):
        return int(converter(source_offset_us))
    speed = max(1, int(getattr(segment, "speed_percent", 100)))
    return (int(source_offset_us) * 100 + speed // 2) // speed


def _map_layer_interval(
    segment: Any, placement: Any, event: dict[str, Any], override: dict[str, Any], layer: str
) -> tuple[int, int] | None:
    origin_in, origin_out = override.get("timing_source_bounds", (segment.in_us, segment.out_us))
    start = max(int(event[f"{layer}_source_in_us"]), origin_in)
    end = min(int(event[f"{layer}_source_out_us"]), origin_out)
    if end <= start:
        return None
    origin_output = placement.output_in_us - _source_offset_to_output_us(segment, segment.in_us - origin_in)
    base_start = origin_output + _source_offset_to_output_us(segment, start - origin_in)
    base_end = origin_output + _source_offset_to_output_us(segment, end - origin_in)
    # 旧的统一偏移仍同时作用于两层；新字段只作用于指定层。
    start_offset = int(override.get("start_offset_us", 0)) + int(override.get(f"{layer}_start_offset_us", 0))
    end_offset = int(override.get("end_offset_us", 0)) + int(override.get(f"{layer}_end_offset_us", 0))
    origin_end = origin_output + _source_offset_to_output_us(segment, origin_out - origin_in)
    output_start = max(placement.output_in_us, origin_output, base_start + start_offset)
    output_end = min(placement.output_out_us, origin_end, base_end + end_offset)
    return (output_start, output_end) if output_end > output_start else None


def map_subtitle_events(cut: Any) -> list[MappedSubtitleEvent]:
    """按当前成片时间轴映射事件，并把逐条偏移限制在承载片段内。"""

    from .timeline import build_timeline

    packaging = normalize_packaging(cut.packaging)
    mapped: list[MappedSubtitleEvent] = []
    for placement in build_timeline(cut):
        segment = placement.segment
        for event in packaging["subtitle_events"]:
            if event["source_file"] != segment.source_file:
                continue
            source_start = max(int(event["source_in_us"]), segment.in_us)
            source_end = min(int(event["source_out_us"]), segment.out_us)
            key = subtitle_instance_key(event["id"], segment.id)
            override = packaging["subtitle_instance_overrides"].get(key, {})
            sticker_interval = _map_layer_interval(segment, placement, event, override, "sticker")
            text_interval = _map_layer_interval(segment, placement, event, override, "new_text")
            if sticker_interval is None and text_interval is None:
                continue
            if source_end <= source_start:
                # An inherited offset may put the whole layer in the other
                # child. Keep the actual cue's provenance for the editor label.
                source_start, source_end = int(event["source_in_us"]), int(event["source_out_us"])
            # 兼容旧调用方：主区间覆盖仍采用源事件区间；实际渲染改用各层区间。
            shared = sticker_interval or text_interval
            assert shared is not None
            mapped.append(
                MappedSubtitleEvent(
                    event_id=str(event["id"]),
                    instance_key=key,
                    segment_id=segment.id,
                    source_file=segment.source_file,
                    source_in_us=source_start,
                    source_out_us=source_end,
                    output_in_us=shared[0],
                    output_out_us=shared[1],
                    sticker_output_in_us=(sticker_interval or shared)[0],
                    sticker_output_out_us=(sticker_interval or shared)[1],
                    new_text_output_in_us=(text_interval or shared)[0],
                    new_text_output_out_us=(text_interval or shared)[1],
                    text=str(override.get("text", event["text"])),
                    source_kind=str(event.get("source_kind", "manual")),
                    # A clipped-away layer must not borrow the surviving
                    # layer's fallback timestamps and reappear after editing.
                    sticker_enabled=sticker_interval is not None and bool(
                        override.get("sticker_enabled", event["sticker_enabled"])
                    ),
                    new_text_enabled=text_interval is not None and bool(
                        override.get("new_text_enabled", event["new_text_enabled"])
                    ),
                    sticker_box=deepcopy(
                        override.get("sticker_box", event.get("sticker_box"))
                    ),
                )
            )
    return sorted(mapped, key=lambda item: (item.output_in_us, item.output_out_us, item.event_id))


def map_audio_items(cut: Any) -> list[MappedAudioItem]:
    """音频绑定到片段和源时间；剪掉锚点时返回“需重新安排”而不漂移。"""

    from .timeline import build_timeline

    packaging = normalize_packaging(cut.packaging)
    placements = {placement.segment.id: placement for placement in build_timeline(cut)}
    result: list[MappedAudioItem] = []
    for item in packaging["audio_items"]:
        segment_id = str(item["anchor_segment_id"])
        placement = placements.get(segment_id)
        offset = int(item["source_offset_us"])
        if placement is None:
            result.append(
                MappedAudioItem(
                    item_id=str(item["id"]),
                    kind=str(item["kind"]),
                    file_path=str(item["file_path"]),
                    anchor_segment_id=segment_id,
                    output_in_us=None,
                    enabled=bool(item["enabled"]),
                    mute_original=bool(item["mute_original"]),
                    volume=float(item.get("volume", 1.0)),
                    needs_rearrangement=True,
                    reason="承载片段已被移除。",
                )
            )
            continue
        segment = placement.segment
        if "anchor_source_us" in item:
            offset = int(item["anchor_source_us"]) - segment.in_us
        if offset < 0 or offset >= segment.out_us - segment.in_us:
            result.append(
                MappedAudioItem(
                    item_id=str(item["id"]),
                    kind=str(item["kind"]),
                    file_path=str(item["file_path"]),
                    anchor_segment_id=segment_id,
                    output_in_us=None,
                    enabled=bool(item["enabled"]),
                    mute_original=bool(item["mute_original"]),
                    volume=float(item.get("volume", 1.0)),
                    needs_rearrangement=True,
                    reason="锚点已落在当前片段外，请重新安排。",
                )
            )
            continue
        output_in = placement.output_in_us + _source_offset_to_output_us(segment, offset)
        duration_us = int(item.get("duration_us", 0))
        if duration_us > 0 and output_in + duration_us > placement.output_out_us:
            result.append(
                MappedAudioItem(
                    item_id=str(item["id"]),
                    kind=str(item["kind"]),
                    file_path=str(item["file_path"]),
                    anchor_segment_id=segment_id,
                    output_in_us=None,
                    enabled=bool(item["enabled"]),
                    mute_original=bool(item["mute_original"]),
                    volume=float(item.get("volume", 1.0)),
                    needs_rearrangement=True,
                    reason="声音时长已越出承载片段，请裁短或重新安排。",
                )
            )
            continue
        result.append(
            MappedAudioItem(
                item_id=str(item["id"]),
                kind=str(item["kind"]),
                file_path=str(item["file_path"]),
                anchor_segment_id=segment_id,
                output_in_us=output_in,
                enabled=bool(item["enabled"]),
                mute_original=bool(item["mute_original"]),
                volume=float(item.get("volume", 1.0)),
                needs_rearrangement=False,
            )
        )
    return result
