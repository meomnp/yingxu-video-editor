"""严格重编码导出。

为了让每一个接缝都落在同一条输出时间轴，第一阶段不使用 stream copy：
每段通过 trim/atrim 后规范为 H.264 + AAC，再由 concat 过滤器拼接。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable
from uuid import uuid4

from .errors import ExportCancelled, ExportError
from .ffmpeg import ffmpeg_binary, probe_media, run_ffmpeg
from .models import Cut, ProjectDocument
from .packaging import map_audio_items, map_subtitle_events, normalize_packaging
from .paths import is_within, resolve_excluded_dirs, resolve_media_root, safe_resolve_media_path
from .resources import ResourceMeter, policy_for_mode
from .timeline import build_render_timeline, total_duration_us
from .narration_mix import apply_narration_render
from .export_guard import ExportResourceGuard


@dataclass(frozen=True, slots=True)
class ExportSettings:
    width: int | None = None
    height: int | None = None
    fps_num: int | None = None
    fps_den: int | None = None
    crf: int = 18
    preset: str = "medium"
    minimum_free_bytes: int = 256 * 1024 * 1024
    include_packaging: bool = False


@dataclass(frozen=True, slots=True)
class ExportResult:
    output_path: Path
    expected_duration_us: int
    observed_duration_us: int
    frame_duration_us: int


def _seconds(value_us: int) -> str:
    return f"{value_us / 1_000_000:.6f}"


def _enable_interval(start_us: int, end_us: int) -> str:
    # concat's timebase is microseconds. Evaluating 900000 * 1e-6 as a
    # double can yield 0.8999999999999999: plain gte(t,0.9) loses a frame.
    # Compare the original discrete ticks, with the same half-open semantics.
    ticks = "round(t*1000000)"
    return f"gte({ticks},{start_us})*lt({ticks},{end_us})"


def _safe_filename(text: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" ._")
    safe = safe[:100].rstrip(" .") or "未命名切片"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return f"_{safe}" if safe.split(".", 1)[0].upper() in reserved else safe


def _output_profile(document: ProjectDocument, cut: Cut, settings: ExportSettings) -> tuple[int, int, int, int]:
    first = document.source_for(cut.segments[0].source_file)
    width = settings.width or first.width or 1280
    height = settings.height or first.height or 720
    if width <= 0 or height <= 0:
        raise ExportError("无法确定输出画面尺寸。")
    width -= width % 2
    height -= height % 2
    if width < 2 or height < 2:
        raise ExportError("输出画面尺寸必须为正偶数。")
    fps_num = settings.fps_num or first.fps_num or 30
    fps_den = settings.fps_den or first.fps_den or 1
    if fps_num <= 0 or fps_den <= 0:
        raise ExportError("输出帧率无效。")
    return width, height, fps_num, fps_den


def export_directory(media_root: str | Path) -> Path:
    root = resolve_media_root(media_root)
    directory = root / "映序导出"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        resolved = directory.resolve(strict=True)
    except OSError as exc:
        raise ExportError("无法创建导出文件夹。", detail=str(directory)) from exc
    if not is_within(resolved, root):
        raise ExportError("导出文件夹不能通过链接离开所选素材文件夹。")
    return resolved


def default_export_path(
    document: ProjectDocument, cut_id: str | None = None, *, packaged: bool = False,
    directory: str | Path | None = None,
) -> Path:
    cut = document.get_cut(cut_id or document.active_cut.id)
    ordinal = next(index for index, item in enumerate(document.cuts, 1) if item.id == cut.id)
    suffix = "_包装版" if packaged else ""
    folder = Path(directory) if directory is not None else export_directory(document.media_root)
    candidate = folder / f"{ordinal:02d}{suffix}.mp4"
    return next_available_export_path(candidate)


def next_available_export_path(candidate: str | Path) -> Path:
    candidate = Path(candidate)
    number = 2
    base = candidate
    while os.path.lexists(candidate):
        candidate = base.with_stem(f"{base.stem}_{number}")
        number += 1
    return candidate


def _validate_output_path(
    document: ProjectDocument,
    target: str | Path | None,
    *,
    internal_cache: bool,
    internal_cache_directory: str = "previews",
    packaged: bool = False,
    cut_id: str | None = None,
    approved_output_directory: str | Path | None = None,
) -> Path:
    root = resolve_media_root(document.media_root)
    if internal_cache:
        if internal_cache_directory not in {"previews", "proxies"}:
            raise ExportError("内部预览缓存目录无效。")
        allowed_dir = root / ".local_slice_assistant" / internal_cache_directory
        try:
            allowed_dir.mkdir(parents=True, exist_ok=True)
            allowed_dir = allowed_dir.resolve(strict=True)
        except OSError as exc:
            raise ExportError("无法创建接缝预览缓存。", detail=str(exc)) from exc
    elif approved_output_directory is not None:
        try:
            allowed_dir = Path(approved_output_directory).resolve(strict=True)
            if not allowed_dir.is_dir():
                raise OSError("not a directory")
        except OSError as exc:
            raise ExportError("所选输出文件夹不存在或不可用。") from exc
    else:
        allowed_dir = export_directory(root)
    output = (
        Path(target).expanduser()
        if target is not None
        else default_export_path(document, cut_id, packaged=packaged)
    )
    if output.suffix.lower() != ".mp4":
        raise ExportError("第一阶段导出文件必须是 .mp4。")
    try:
        resolved = output.resolve(strict=False)
    except OSError as exc:
        raise ExportError("导出路径无效。", detail=str(output)) from exc
    if not is_within(resolved, allowed_dir):
        location = "缓存目录" if internal_cache else "已选择的输出目录"
        raise ExportError(f"导出文件必须保存在{location}内。", detail=str(resolved))
    if os.path.lexists(output) or resolved.exists():
        raise ExportError("同名导出文件已存在，请使用新文件名。不会覆盖已有成片。", detail=str(output))
    if any(resolved == (root / name).resolve(strict=False) for name in document.sources):
        raise ExportError("不能使用原素材的路径作为导出文件。")
    return resolved


def _publish_new_export(temporary: Path, target: Path) -> None:
    """Publish atomically without replacing a file created during encoding."""
    if os.name == "nt":
        os.rename(temporary, target)  # Windows rename fails if target exists.
    else:
        os.link(temporary, target)  # POSIX rename would overwrite the target.
        temporary.unlink()


def _check_free_space(directory: Path, minimum_free_bytes: int) -> None:
    try:
        free = shutil.disk_usage(directory).free
    except OSError as exc:
        raise ExportError("无法检查导出磁盘空间。", detail=str(directory)) from exc
    if free < minimum_free_bytes:
        raise ExportError(
            "导出磁盘可用空间不足。",
            detail=f"至少需要 {minimum_free_bytes // (1024 * 1024)} MiB，可用 {free // (1024 * 1024)} MiB。",
        )


def _filter_value(value: str) -> str:
    """转义单个 FFmpeg filter 值；命令本身始终以参数数组调用。"""

    return (
        str(value)
        .replace("\\", "/")
        .replace("'", r"\'")
        .replace(":", r"\:")
        .replace(",", r"\,")
        .replace("[", r"\[")
        .replace("]", r"\]")
        .replace("%", r"\%")
        .replace("\n", r"\n")
    )


def _default_font_path() -> Path:
    candidates = (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
    )
    candidate = next((item for item in candidates if item.is_file()), None)
    if not candidate:
        raise ExportError("找不到可用于绘制字幕的系统字体。请在字幕样式中选择字体文件。")
    return candidate


def _font_expression(path: str | None) -> str:
    target = Path(path) if path else _default_font_path()
    if not target.is_file():
        raise ExportError("字幕或字卡引用的字体文件不存在。", detail=str(target))
    return f"fontfile='{_filter_value(os.fspath(target))}'"


def _drawtext_expression(
    *,
    text: str,
    font_path: str | None,
    font_size: int,
    color: str,
    x: str,
    y: str,
    enable: str | None = None,
) -> str:
    parts = [
        "drawtext=" + _font_expression(font_path),
        f"text='{_filter_value(text)}'",
        f"fontcolor={color}",
        f"fontsize={font_size}",
        f"x={x}",
        f"y={y}",
    ]
    if enable:
        parts.append(f"enable='{enable}'")
    return ":".join(parts)


def _card_video_filter(
    card: dict[str, object],
    *,
    input_index: int | None,
    duration_us: int,
    width: int,
    height: int,
    fps_num: int,
    fps_den: int,
) -> str:
    duration = _seconds(duration_us)
    kind = str(card.get("kind", "text"))
    if kind == "image":
        if input_index is None:
            raise ExportError("图片字卡缺少输入媒体。")
        base = (
            f"[{input_index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih):color=black,"
            f"fps={fps_num}/{fps_den},trim=duration={duration},setpts=PTS-STARTPTS"
        )
    else:
        base = (
            f"color=c={card.get('background_color', 'black')}:s={width}x{height}:"
            f"r={fps_num}/{fps_den}:d={duration}"
        )
    text = str(card.get("text", "")).strip()
    if text:
        font_size = int(card.get("font_size", max(28, height // 14)))
        base += "," + _drawtext_expression(
            text=text,
            font_path=(
                str(card["font_path"])
                if isinstance(card.get("font_path"), str)
                else None
            ),
            font_size=font_size,
            color=str(card.get("text_color", "white")),
            x="(w-text_w)/2",
            y="(h-text_h)/2",
        )
    return base


def _append_subtitle_filters(
    filters: list[str],
    video_label: str,
    *,
    cut: Cut,
    inputs: list[str],
    width: int,
    height: int,
) -> str:
    """用与工程映射相同的 output 时间区间叠加遮挡和新字幕。"""

    packaging = normalize_packaging(cut.packaging)
    style = packaging["caption_style"]
    sticker = style["sticker"]
    text_style = style["new_text"]
    current = video_label
    serial = 0
    transparent_crop_cache: dict[Path, tuple[int, int, int, int] | None] = {}
    mapped_events = list(map_subtitle_events(cut))
    image_labels: dict[str, str] = {}
    image_events = [event for event in mapped_events if bool(sticker["enabled"]) and event.sticker_enabled]
    if sticker.get("image_path") and image_events:
        path = Path(str(sticker["image_path"]))
        if not path.is_file():
            raise ExportError("透明贴纸图片不存在，请重新选择。", detail=str(path))
        image_input = inputs.count("-i")
        inputs.extend(["-loop", "1", "-i", os.fspath(path)])
        # Reuse one image decoder for the entire cut, including repeated episodes.
        labels = [f"[captionimage{index}]" for index in range(len(image_events))]
        image_labels = {event.instance_key: label for event, label in zip(image_events, labels)}
        shared = f"[{image_input}:v:0]format=rgba,colorchannelmixer=aa={float(sticker['opacity']):.6f}"
        filters.append(shared + (f",split={len(labels)}" if len(labels) > 1 else ",null") + "".join(labels))
    for event in mapped_events:
        if bool(sticker["enabled"]) and event.sticker_enabled:
            # The timeline uses [in, out). An inclusive end leaks the cover
            # into the first frame of the following subtitle or gap.
            enable = _enable_interval(event.sticker_output_in_us, event.sticker_output_out_us)
            next_label = f"[vpack{serial}]"
            serial += 1
            preset = str(sticker.get("preset", "pure_white"))
            color = {"pure_white": "white", "warm_white_soft": "#fff4dc", "dark_gray": "#303030"}.get(preset, str(sticker["color"]))
            # 画面检测事件携带自己的文字包围框；手工／SRT 事件仍用全局样式框。
            geometry = event.sticker_box or sticker
            image_path = sticker.get("image_path")
            if image_path:
                path = Path(str(image_path))
                if not path.is_file():
                    raise ExportError("透明贴纸图片不存在，请重新选择。", detail=str(path))
                crop = transparent_crop_cache.get(path)
                if path not in transparent_crop_cache:
                    crop = _transparent_content_crop(path)
                    transparent_crop_cache[path] = crop
                image_source = image_labels[event.instance_key]
                sticker_width = max(2, round(width * float(geometry["width"])))
                sticker_height = max(2, round(height * float(geometry["height"])))
                sticker_x = round(width * float(geometry["x"]))
                sticker_y = round(height * float(geometry["y"]))
                crop_filter = (
                    f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else ""
                )
                # 对横向装饰贴纸保留两端纹样，只延展安静的中间区域。整张等比
                # 拉伸会让长字幕两端的云纹、边框明显失真，正是实片中最刺眼的问题。
                source_width = crop[0] if crop else sticker_width
                source_height = crop[1] if crop else sticker_height
                cap_source_width = max(1, round(source_width * 0.24))
                cap_output_width = min(
                    max(2, round(sticker_height * cap_source_width / max(1, source_height))),
                    max(1, sticker_width // 2),
                )
                center_output_width = sticker_width - cap_output_width * 2
                if crop is None or center_output_width < 2 or sticker.get("fit_mode") == "stretch":
                    image_label = f"[vsticker{serial}]"
                    filters.append(
                        f"{image_source}format=rgba,{crop_filter}"
                        f"scale={sticker_width}:{sticker_height},setsar=1{image_label}"
                    )
                    filters.append(
                        f"{current}{image_label}overlay=x={sticker_x}:y={sticker_y}:format=auto:"
                        f"enable='{enable}'{next_label}"
                    )
                else:
                    left_label = f"[vsticker{serial}l]"
                    center_label = f"[vsticker{serial}c]"
                    right_label = f"[vsticker{serial}r]"
                    first_overlay = f"[vpack{serial}]"
                    second_overlay = f"[vpack{serial + 1}]"
                    serial += 2
                    center_source_width = max(1, source_width - cap_source_width * 2)
                    filters.append(
                        f"{image_source}format=rgba,{crop_filter}split=3"
                        f"{left_label}{center_label}{right_label}"
                    )
                    filters.append(
                        f"{left_label}crop={cap_source_width}:{source_height}:0:0,"
                        f"scale={cap_output_width}:{sticker_height},setsar=1[vstickerleft{serial}]"
                    )
                    filters.append(
                        f"{center_label}crop={center_source_width}:{source_height}:{cap_source_width}:0,"
                        f"scale={center_output_width}:{sticker_height},setsar=1[vstickercenter{serial}]"
                    )
                    filters.append(
                        f"{right_label}crop={cap_source_width}:{source_height}:"
                        f"{source_width - cap_source_width}:0,scale={cap_output_width}:{sticker_height},"
                        f"setsar=1[vstickerright{serial}]"
                    )
                    filters.append(
                        f"{current}[vstickerleft{serial}]overlay=x={sticker_x}:y={sticker_y}:format=auto:"
                        f"enable='{enable}'{first_overlay}"
                    )
                    filters.append(
                        f"{first_overlay}[vstickercenter{serial}]overlay=x={sticker_x + cap_output_width}:y={sticker_y}:format=auto:"
                        f"enable='{enable}'{second_overlay}"
                    )
                    filters.append(
                        f"{second_overlay}[vstickerright{serial}]overlay=x={sticker_x + cap_output_width + center_output_width}:y={sticker_y}:format=auto:"
                        f"enable='{enable}'{next_label}"
                    )
                current = next_label
            # Soft preset is a deterministic, layered geometry: a solid centre and
            # two lower-alpha outer bands.  It neither downloads nor generates an image.
            feather = min(0.08, max(0.0, float(sticker.get("feather", 0.012))))
            layers = [(0.0, 1.0)] if preset != "warm_white_soft" else [(feather * 1.5, 0.14), (feather * 0.75, 0.32), (0.0, 1.0)]
            if image_path:
                layers = []
            layer_current = current
            for index, (expand, alpha) in enumerate(layers):
                out_label = next_label if index == len(layers) - 1 else f"[vpack{serial}]"
                if index != len(layers) - 1:
                    serial += 1
                x = max(0.0, float(geometry["x"]) - expand)
                y = max(0.0, float(geometry["y"]) - expand)
                box_width = min(1.0 - x, float(geometry["width"]) + expand * 2)
                box_height = min(1.0 - y, float(geometry["height"]) + expand * 2)
                radius = min(box_width, box_height) * min(0.45, max(0.0, float(sticker.get("corner_radius", 0.0))))
                # FFmpeg drawbox has no rounded-rectangle primitive.  Two
                # overlapping rectangles make a deterministic octagonal corner,
                # while radius=0 remains an exact rectangle.
                pieces = [(x, y, box_width, box_height)] if radius < 0.001 else [
                    (x + radius, y, box_width - radius * 2, box_height),
                    (x, y + radius, box_width, box_height - radius * 2),
                ]
                for piece_index, (px, py, pw, ph) in enumerate(pieces):
                    piece_label = out_label if piece_index == len(pieces) - 1 else f"[vpack{serial}]"
                    if piece_index != len(pieces) - 1:
                        serial += 1
                    filters.append(
                        f"{layer_current}drawbox=x=iw*{px:.6f}:y=ih*{py:.6f}:"
                        f"w=iw*{pw:.6f}:h=ih*{ph:.6f}:"
                        f"color={color}@{float(sticker['opacity']) * alpha:.6f}:"
                        f"t=fill:enable='{enable}'{piece_label}"
                    )
                    layer_current = piece_label
                layer_current = out_label
            current = next_label
        if bool(text_style["enabled"]) and event.new_text_enabled:
            enable = _enable_interval(event.new_text_output_in_us, event.new_text_output_out_us)
            alignment = str(text_style["alignment"])
            # 检测生成的事件可能各自有不同的字幕宽度与位置。新字幕必须
            # 跟随同一张贴纸，不能仍然落到全局默认框上。
            text_geometry = event.sticker_box or sticker
            if alignment == "left":
                x = f"w*{float(text_geometry['x']) + 0.02:.6f}"
            elif alignment == "right":
                x = f"w*{float(text_geometry['x']) + float(text_geometry['width']) - 0.02:.6f}-text_w"
            else:
                x = f"w*{float(text_geometry['x']) + float(text_geometry['width']) / 2:.6f}-text_w/2"
            y = f"h*{float(text_geometry['y']) + float(text_style['margin_y']):.6f}"
            next_label = f"[vpack{serial}]"
            serial += 1
            filters.append(
                f"{current}"
                + _drawtext_expression(
                    text=event.text,
                    font_path=(
                        str(text_style["font_path"])
                        if isinstance(text_style.get("font_path"), str)
                        else None
                    ),
                    font_size=int(text_style["font_size"]),
                    color=str(text_style["color"]),
                    x=x,
                    y=y,
                    enable=enable,
                )
                + next_label
            )
            current = next_label
    return current


def _transparent_content_crop(path: Path) -> tuple[int, int, int, int] | None:
    """读取 PNG/WebP 的 alpha 有效像素边界，避免透明留白压缩可见贴纸。"""

    try:
        completed = subprocess.run(
            [
                ffmpeg_binary(), "-hide_banner", "-v", "verbose", "-loop", "1", "-i", os.fspath(path),
                "-vf", "alphaextract,cropdetect=limit=0.01:round=2:reset=1",
                "-frames:v", "3", "-f", "null", "-",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", completed.stderr)
    if not matches:
        return None
    width, height, x, y = (int(value) for value in matches[-1])
    return (width, height, x, y) if width > 1 and height > 1 else None


def _audio_sample(time_us: int) -> int:
    """One rounding rule for every 48 kHz audio placement and mute boundary."""
    return (int(time_us) * 48000 + 500000) // 1000000


def _append_mute_windows(filters, current, intervals, total_samples):
    """Mute exact sample ranges, not whole decoded audio frames.

    Merge overlaps before splitting the bed so one voice cannot bring the
    original dialogue back while another voice's mute window is still active.
    """
    merged = []
    for start, end in sorted(intervals):
        start, end = max(0, start), min(total_samples, end)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    if not merged:
        return current
    parts = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            parts.append((cursor, start, False))
        parts.append((start, end, True))
        cursor = end
    if cursor < total_samples:
        parts.append((cursor, total_samples, False))
    filters.append(f"{current}asplit={len(parts)}" + "".join(f"[bedpart{i}]" for i in range(len(parts))))
    for index, (start, end, mute) in enumerate(parts):
        filters.append(f"[bedpart{index}]atrim=start_sample={start}:end_sample={end},"
                       "asetpts=N/SR/TB" + (",volume=0" if mute else "") + f"[bedtrim{index}]")
    filters.append("".join(f"[bedtrim{i}]" for i in range(len(parts)))
                   + f"concat=n={len(parts)}:v=0:a=1[amutebed]")
    return "[amutebed]"


def _filter_graph(
    document: ProjectDocument,
    cut: Cut,
    *,
    width: int,
    height: int,
    fps_num: int,
    fps_den: int,
    include_packaging: bool,
) -> tuple[list[str], str]:
    root = resolve_media_root(document.media_root)
    exclusions = resolve_excluded_dirs(root)
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    render_items = build_render_timeline(cut) if include_packaging else ()
    if not include_packaging:
        # 保持第一阶段粗剪的顺序和无包装导出语义，倍速仍是工程本身的一部分。
        from .timeline import RenderPlacement

        cursor = 0
        render_items = []
        for segment in cut.segments:
            render_items.append(
                RenderPlacement(
                    kind="segment",
                    output_in_us=cursor,
                    output_out_us=cursor + segment.duration_us,
                    segment=segment,
                )
            )
            cursor += segment.duration_us

    for item_index, placement in enumerate(render_items):
        video_label = f"v{item_index}"
        audio_label = f"a{item_index}"
        if placement.segment is not None:
            segment = placement.segment
            source = document.source_for(segment.source_file)
            _relative, source_path = safe_resolve_media_path(
                root, segment.source_file, exclusions=exclusions
            )
            input_index = len(
                [value for value in inputs if value == "-i"]
            )
            inputs.extend(["-i", os.fspath(source_path)])
            start = _seconds(segment.in_us)
            end = _seconds(segment.out_us)
            output_duration = _seconds(segment.duration_us)
            speed = segment.speed_percent
            filters.append(
                f"[{input_index}:v:0]"
                f"trim=start={start}:end={end},"
                "setpts=PTS-STARTPTS,"
                f"setpts=PTS*100/{speed},"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih):color=black,"
                f"fps={fps_num}/{fps_den},setsar=1[{video_label}]"
            )
            if source.has_audio and segment.original_audio == "keep":
                filters.append(
                    f"[{input_index}:a:0]"
                    f"atrim=start={start}:end={end},"
                    "asetpts=PTS-STARTPTS,"
                    f"atempo={speed / 100:.6f},"
                    "aresample=48000,"
                    f"apad,atrim=duration={output_duration},"
                    f"aformat=sample_rates=48000:channel_layouts=stereo[{audio_label}]"
                )
            else:
                filters.append(
                    "anullsrc=r=48000:cl=stereo,"
                    f"atrim=duration={output_duration},asetpts=PTS-STARTPTS[{audio_label}]"
                )
        else:
            card = placement.title_card or {}
            duration = placement.output_out_us - placement.output_in_us
            image_input: int | None = None
            if str(card.get("kind", "text")) == "image":
                path = Path(str(card.get("image_path", "")))
                if not path.is_file():
                    raise ExportError("图片字卡文件不存在。", detail=str(path))
                image_input = len([value for value in inputs if value == "-i"])
                inputs.extend(
                    [
                        "-loop",
                        "1",
                        "-framerate",
                        f"{fps_num}/{fps_den}",
                        "-t",
                        _seconds(duration),
                        "-i",
                        os.fspath(path),
                    ]
                )
            filters.append(
                _card_video_filter(
                    card,
                    input_index=image_input,
                    duration_us=duration,
                    width=width,
                    height=height,
                    fps_num=fps_num,
                    fps_den=fps_den,
                )
                + f"[{video_label}]"
            )
            filters.append(
                "anullsrc=r=48000:cl=stereo,"
                f"atrim=duration={_seconds(duration)},asetpts=PTS-STARTPTS[{audio_label}]"
            )
        labels.extend([f"[{video_label}]", f"[{audio_label}]"])

    filters.append(
        "".join(labels)
        + f"concat=n={len(render_items)}:v=1:a=1[vconcat][aconcat]"
    )
    video_current = "[vconcat]"
    audio_current = "[aconcat]"

    if include_packaging:
        try:
            audio_current = apply_narration_render(filters, inputs, audio_current, cut, document)
        except (ValueError, OSError) as exc:
            raise ExportError("解说混音尚未就绪，不能导出缺少解说的包装成片。", detail=str(exc)) from exc
        video_current = _append_subtitle_filters(
            filters, video_current, cut=cut, inputs=inputs, width=width, height=height
        )
        items_by_id = {
            str(item["id"]): item
            for item in normalize_packaging(cut.packaging)["audio_items"]
        }
        audio_labels: list[str] = []
        mute_intervals: list[tuple[int, int]] = []
        for mapped in map_audio_items(cut):
            if not mapped.enabled:
                continue
            if mapped.needs_rearrangement or mapped.output_in_us is None:
                raise ExportError(
                    "有启用的配音／音乐失去了承载片段，请在工程中重新安排后再导出。",
                    detail=mapped.reason or mapped.item_id,
                )
            raw_item = items_by_id[mapped.item_id]
            path = Path(mapped.file_path)
            if not path.is_file():
                raise ExportError("配音／音乐文件不存在。", detail=str(path))
            duration_us = raw_item.get("duration_us")
            source_in_us = int(raw_item.get("source_in_us", 0))
            if not isinstance(duration_us, int) or duration_us <= 0:
                raise ExportError(
                    "配音／音乐缺少已检测的时长，请重新导入该文件。",
                    detail=str(path),
                )
            input_index = len([value for value in inputs if value == "-i"])
            inputs.extend(["-i", os.fspath(path)])
            label = f"apack{len(audio_labels)}"
            start_sample = _audio_sample(mapped.output_in_us)
            end_sample = _audio_sample(mapped.output_in_us + duration_us)
            filters.append(
                f"[{input_index}:a:0]"
                f"atrim=start={_seconds(source_in_us)}:duration={_seconds(duration_us)},"
                "asetpts=PTS-STARTPTS,"
                "aresample=48000,"
                f"apad,atrim=end_sample={end_sample - start_sample},"
                f"aformat=sample_rates=48000:channel_layouts=stereo,volume={mapped.volume:.6f},"
                f"adelay={start_sample}S:all=1[{label}]"
            )
            audio_labels.append(f"[{label}]")
            if mapped.kind == "voiceover" and mapped.mute_original:
                mute_intervals.append(
                    (start_sample, end_sample)
                )
        audio_current = _append_mute_windows(
            filters, audio_current, mute_intervals, _audio_sample(total_duration_us(cut))
        )
        if audio_labels:
            filters.append(
                f"{audio_current}{''.join(audio_labels)}"
                f"amix=inputs={len(audio_labels) + 1}:duration=first:dropout_transition=0:normalize=0,"
                "alimiter=limit=0.95:level=0:latency=1[aout]"
            )
            audio_current = "[aout]"

    filters.append(f"{video_current}null[vout]")
    if audio_current != "[aout]":
        filters.append(f"{audio_current}anull[aout]")
    return inputs, ";".join(filters)


def export_cut(
    document: ProjectDocument,
    *,
    cut_id: str | None = None,
    output_path: str | Path | None = None,
    settings: ExportSettings | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[str], None] | None = None,
    internal_cache: bool = False,
    internal_cache_directory: str = "previews",
    resource_meter: ResourceMeter | None = None,
    approved_output_directory: str | Path | None = None,
) -> ExportResult:
    """从不可变片段快照导出；失败或取消时不会碰最终文件。"""

    if cancel_event and cancel_event.is_set():
        raise ExportCancelled("导出已取消。")
    document.validate()
    cut = document.get_cut(cut_id or document.active_cut.id)
    snapshot = Cut.from_dict(cut.to_dict())
    export_settings = settings or ExportSettings()
    if export_settings.include_packaging:
        ids = {segment.id for segment in snapshot.segments}
        pending_cards = [card for card in normalize_packaging(snapshot.packaging)["title_cards"]
                         if card.get("enabled", True) and card.get("position", "after") != "end"
                         and card.get("anchor_segment_id") not in ids]
        if pending_cards:
            raise ExportError("有字卡或插画需要重新安排。请在“管理声音与字卡”中重新绑定、停用或移除，再导出包装版。")
    target = _validate_output_path(
        document,
        output_path,
        internal_cache=internal_cache,
        internal_cache_directory=internal_cache_directory,
        packaged=export_settings.include_packaging,
        cut_id=cut.id,
        approved_output_directory=approved_output_directory,
    )
    _check_free_space(target.parent, export_settings.minimum_free_bytes)
    width, height, fps_num, fps_den = _output_profile(document, snapshot, export_settings)
    inputs, graph = _filter_graph(
        document,
        snapshot,
        width=width,
        height=height,
        fps_num=fps_num,
        fps_den=fps_den,
        include_packaging=export_settings.include_packaging,
    )
    expected_duration_us = (
        total_duration_us(snapshot)
        if export_settings.include_packaging
        else sum(segment.duration_us for segment in snapshot.segments)
    )
    temporary = target.with_name(f".{target.stem}.{uuid4().hex}.partial.mp4")
    policy = policy_for_mode(str(document.resource_settings.get("mode", "standard")))
    # FFmpeg's automatic decoder threading multiplies with every source/clip.
    # Bound each input decoder and both configurable output processing pools.
    bounded_inputs = []
    for value in inputs:
        if value == "-i":
            bounded_inputs.extend(["-threads", "1"])
        bounded_inputs.append(value)
    arguments = [
        "-nostdin",
        "-y",
        "-filter_complex_threads",
        str(policy.cpu_threads),
        *bounded_inputs,
        "-filter_complex",
        graph,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-t",
        _seconds(expected_duration_us),
        "-c:v",
        "libx264",
        "-threads:v",
        str(policy.cpu_threads),
        "-threads:a",
        "1",
        "-preset",
        export_settings.preset,
        "-crf",
        str(export_settings.crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-fps_mode",
        "cfr",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        os.fspath(temporary),
    ]
    try:
        guard = ExportResourceGuard(target.parent, policy.mode, resource_meter)
        if progress:
            progress(guard.description())
            progress(
                "正在精确重编码、串联片段并叠加可编辑包装…"
                if export_settings.include_packaging
                else "正在精确重编码并串联片段…"
            )
        run_ffmpeg(
            arguments,
            cancel_event=cancel_event,
            progress=progress,
            resource_meter=guard,
            expected_duration_us=expected_duration_us,
        )
        if cancel_event and cancel_event.is_set():
            raise ExportCancelled("导出已取消。")
        observed = probe_media(temporary)
        expected_frame_us = max(1, round(1_000_000 * fps_den / fps_num))
        tolerance_us = max(expected_frame_us, observed.frame_duration_us)
        if abs(observed.duration_us - expected_duration_us) > tolerance_us:
            raise ExportError(
                "导出时长校验失败，未替换最终文件。",
                detail=(
                    f"预期 {expected_duration_us // 1000} ms，"
                    f"实际 {observed.duration_us // 1000} ms"
                ),
            )
        try:
            _publish_new_export(temporary, target)
        except FileExistsError as exc:
            raise ExportError("同名成片在导出期间已被创建；未覆盖它，请换文件名重试。", detail=str(target)) from exc
        except PermissionError as exc:
            raise ExportError(
                "没有写入导出文件的权限；请关闭占用文件或检查文件夹权限。",
                detail=str(target),
            ) from exc
        except OSError as exc:
            raise ExportError("无法保存最终成片；已有文件未改动。", detail=str(exc)) from exc
        return ExportResult(
            output_path=target,
            expected_duration_us=expected_duration_us,
            observed_duration_us=observed.duration_us,
            frame_duration_us=tolerance_us,
        )
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
