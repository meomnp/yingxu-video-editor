"""选定字幕区域的本地检测。

这不是 OCR：它只在用户选中的单个片段和矩形区域内，按亮像素变化提出可能的
出现／消失区间。真实文字、颜色和语义仍由用户在预览里确认并手工填写。

`detect_caption_text_presence` 是另一条、默认使用的文字形态检测路径：它只判断
ROI 中是否存在多笔画、成行排列的文字形状，不读取文字内容，也不把亮度当 OCR。
旧的 ``detect_caption_visibility`` 保留给旧工程的亮度候选兼容，不由新界面调用。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

from .analysis import MICROSECONDS
from .errors import AnalysisCancelled, MediaProbeError
from .ffmpeg import ffmpeg_binary
from .models import ProjectDocument, Segment
from .paths import resolve_excluded_dirs, resolve_media_root, safe_resolve_media_path
from .resources import ResourceMeter


@dataclass(frozen=True, slots=True)
class CaptionVisibilityCandidate:
    source_file: str
    source_in_us: int
    source_out_us: int
    max_bright_ratio: float
    evidence: str


@dataclass(frozen=True, slots=True)
class CaptionDetectionResult:
    source_file: str
    analyzed_in_us: int
    analyzed_out_us: int
    sample_count: int
    candidates: tuple[CaptionVisibilityCandidate, ...]
    note: str


@dataclass(frozen=True, slots=True)
class CaptionTextCandidate:
    source_file: str
    source_in_us: int
    source_out_us: int
    confidence: float
    x_ratio: float
    y_ratio: float
    width_ratio: float
    height_ratio: float
    evidence: str


@dataclass(frozen=True, slots=True)
class CaptionTextDetectionResult:
    source_file: str
    analyzed_in_us: int
    analyzed_out_us: int
    sample_count: int
    candidates: tuple[CaptionTextCandidate, ...]
    note: str


def _seconds(value_us: int) -> str:
    return f"{value_us / MICROSECONDS:.6f}"


def _source_path(document: ProjectDocument, source_file: str) -> Path:
    root = resolve_media_root(document.media_root)
    _relative, path = safe_resolve_media_path(
        root, source_file, exclusions=resolve_excluded_dirs(root)
    )
    return path


def _run_capture(
    command: list[str],
    *,
    cancel_event: Event | None,
    resource_meter: ResourceMeter | None,
) -> bytes:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise MediaProbeError("无法启动字幕选区检测。", detail=str(exc)) from exc
    started = time.monotonic()
    try:
        while True:
            if resource_meter:
                resource_meter.observe(process.pid)
            if cancel_event and cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise AnalysisCancelled("字幕选区检测已取消，已停止本工具启动的 FFmpeg。")
            if time.monotonic() - started > 90:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise MediaProbeError("字幕选区检测超时。")
            try:
                stdout, stderr = process.communicate(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    if resource_meter:
        resource_meter.observe(process.pid)
    if process.returncode:
        raise MediaProbeError(
            "无法读取选定字幕区域的局部画面。",
            detail=stderr.decode("utf-8", errors="replace")[-1600:],
        )
    return stdout


def detect_caption_visibility(
    document: ProjectDocument,
    segment: Segment,
    *,
    x_ratio: float,
    y_ratio: float,
    width_ratio: float,
    height_ratio: float,
    cancel_event: Event | None = None,
    resource_meter: ResourceMeter | None = None,
) -> CaptionDetectionResult:
    """从一个源片段的用户选区提取亮度变化候选，不扫描其他集。"""

    source = document.source_for(segment.source_file)
    if source.width <= 0 or source.height <= 0:
        raise MediaProbeError("无法确定源视频尺寸，不能检测字幕选区。")
    x = max(0, min(source.width - 2, round(float(x_ratio) * source.width)))
    y = max(0, min(source.height - 2, round(float(y_ratio) * source.height)))
    width = max(2, min(source.width - x, round(float(width_ratio) * source.width)))
    height = max(2, min(source.height - y, round(float(height_ratio) * source.height)))
    width -= width % 2
    height -= height % 2
    if width < 2 or height < 2:
        raise MediaProbeError("选定的字幕区域过小。")
    sample_fps = 4
    sample_width, sample_height = 80, 20
    raw = _run_capture(
        [
            ffmpeg_binary(),
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-ss",
            _seconds(segment.in_us),
            "-t",
            _seconds(segment.source_duration_us),
            "-i",
            os.fspath(_source_path(document, segment.source_file)),
            "-vf",
            (
                f"crop={width}:{height}:{x}:{y},fps={sample_fps},"
                f"scale={sample_width}:{sample_height}:flags=area,format=gray"
            ),
            "-f",
            "rawvideo",
            "-",
        ],
        cancel_event=cancel_event,
        resource_meter=resource_meter,
    )
    frame_size = sample_width * sample_height
    frames = [
        raw[offset : offset + frame_size]
        for offset in range(0, len(raw) - frame_size + 1, frame_size)
    ]
    ratios = [sum(value >= 220 for value in frame) / frame_size for frame in frames]
    threshold = 0.03
    candidates: list[CaptionVisibilityCandidate] = []
    start_index: int | None = None
    peak = 0.0
    for index, ratio in enumerate([*ratios, 0.0]):
        active = ratio >= threshold
        if active and start_index is None:
            start_index = index
            peak = ratio
        elif active:
            peak = max(peak, ratio)
        elif start_index is not None:
            source_in = segment.in_us + start_index * MICROSECONDS // sample_fps
            source_out = min(
                segment.out_us,
                segment.in_us + index * MICROSECONDS // sample_fps,
            )
            if source_out > source_in:
                candidates.append(
                    CaptionVisibilityCandidate(
                        source_file=segment.source_file,
                        source_in_us=source_in,
                        source_out_us=source_out,
                        max_bright_ratio=round(peak, 4),
                        evidence=(
                            f"选区 {sample_fps} fps 低分辨率亮像素峰值 {peak:.1%}；"
                            "仅是字幕可能出现／消失候选，需在预览中校准。"
                        ),
                    )
                )
            start_index = None
            peak = 0.0
    return CaptionDetectionResult(
        source_file=segment.source_file,
        analyzed_in_us=segment.in_us,
        analyzed_out_us=segment.out_us,
        sample_count=len(frames),
        candidates=tuple(candidates),
        note=(
            "检测只查看当前片段的用户选区和亮度变化，不识别文字语义；"
            "漏检、误检或彩色字幕都应由用户手工调整。"
        ),
    )


def _text_shape_score(frame: bytes, width: int, height: int) -> float:
    """以局部梯度和多行笔画分布测量“像文字”的程度，绝不识读文字。"""

    if len(frame) != width * height:
        return 0.0
    row_hits = [0] * height
    total = 0
    # 同时看横、纵梯度；实心亮块主要只留下边框，字符会留下多行短笔画。
    for y in range(1, height - 1):
        base = y * width
        for x in range(1, width - 1):
            index = base + x
            edge = abs(frame[index + 1] - frame[index - 1]) + abs(frame[index + width] - frame[index - width])
            if edge >= 105:
                row_hits[y] += 1
                total += 1
    if total < width // 2:
        return 0.0
    active_rows = [count for count in row_hits if count >= max(3, width // 45)]
    row_ratio = len(active_rows) / max(1, height)
    edge_ratio = total / max(1, (width - 2) * (height - 2))
    # 文字通常占 ROI 的一小段高度；大色块、纯背景和孤立物体会被压低。
    if not 0.07 <= row_ratio <= 0.78 or not 0.008 <= edge_ratio <= 0.42:
        return 0.0
    return min(1.0, (row_ratio / 0.22) * 0.55 + (edge_ratio / 0.075) * 0.45)


def _text_shape_bounds(frame: bytes, width: int, height: int) -> tuple[int, int, int, int] | None:
    """返回文字笔画的紧凑边界，不读取或识别任何文字。"""

    if _text_shape_score(frame, width, height) <= 0:
        return None
    # 硬字幕常见“亮字 + 深描边”。仅用梯度会把复杂背景也框进来；这里要求
    # 亮笔画紧邻深色描边，再按行密度挑出连续字行。
    points: list[tuple[int, int]] = []
    for y in range(1, height - 1):
        base = y * width
        for x in range(1, width - 1):
            index = base + x
            center = frame[index]
            neighbors = (
                frame[index - 1], frame[index + 1], frame[index - width], frame[index + width]
            )
            if center >= 185 and min(neighbors) <= 105:
                points.append((x, y))
    if not points:
        return None
    row_counts = [0] * height
    for _x, y in points:
        row_counts[y] += 1
    dense_rows = [count >= max(3, width // 55) for count in row_counts]
    runs: list[tuple[int, int, int]] = []
    start: int | None = None
    for y, value in enumerate([*dense_rows, False]):
        if value and start is None:
            start = y
        elif not value and start is not None:
            score = sum(row_counts[start:y])
            runs.append((score, start, y))
            start = None
    if not runs:
        return None
    # 不能只挑最密的一行：常见的两行硬字幕会让第二行完全漏掉。仅合并
    # 与主行相近、且垂直距离仍像同一字幕块的行，避免把 ROI 内远处的亮物体
    # 或角色轮廓一并吞进贴纸框。
    main_score, main_top, main_bottom = max(runs)
    nearby_runs = [
        (top, bottom)
        for score, top, bottom in runs
        if score >= main_score * 0.32
        and top <= main_bottom + 16
        and bottom >= main_top - 16
    ]
    top = min(item[0] for item in nearby_runs)
    bottom = max(item[1] for item in nearby_runs)
    line_points = [(x, y) for x, y in points if top <= y < bottom]
    xs, ys = zip(*line_points)
    # 预留描边与压缩误差，导出贴纸不会刚好压住文字的一条边。
    return (
        max(0, min(xs) - 5),
        max(0, min(ys) - 4),
        min(width, max(xs) + 6),
        min(height, max(ys) + 5),
    )


def detect_caption_text_presence(
    document: ProjectDocument,
    segment: Segment,
    *,
    x_ratio: float,
    y_ratio: float,
    width_ratio: float,
    height_ratio: float,
    cancel_event: Event | None = None,
    resource_meter: ResourceMeter | None = None,
) -> CaptionTextDetectionResult:
    """检测当前片段 ROI 中的文字形态，返回可校准的遮挡区间。

    仅读取选中的一个片段、缩放后的 ROI。允许一整集（最长六分钟）一键检查；
    读取的是缩小的字幕小区域，不会加载整幅视频或后台常驻模型。
    """

    if segment.source_duration_us > 360 * MICROSECONDS:
        raise MediaProbeError("文字检测一次最多分析 6 分钟；请先切成更短的片段。")
    source = document.source_for(segment.source_file)
    if source.width <= 0 or source.height <= 0:
        raise MediaProbeError("无法确定源视频尺寸，不能检测字幕选区。")
    x = max(0, min(source.width - 2, round(float(x_ratio) * source.width)))
    y = max(0, min(source.height - 2, round(float(y_ratio) * source.height)))
    width = max(2, min(source.width - x, round(float(width_ratio) * source.width)))
    height = max(2, min(source.height - y, round(float(height_ratio) * source.height)))
    width -= width % 2
    height -= height % 2
    if width < 2 or height < 2:
        raise MediaProbeError("选定的字幕区域过小。")
    sample_fps, sample_width, sample_height = 6, 240, 72
    raw = _run_capture(
        [
            ffmpeg_binary(), "-hide_banner", "-nostdin", "-v", "error",
            "-ss", _seconds(segment.in_us), "-t", _seconds(segment.source_duration_us),
            "-i", os.fspath(_source_path(document, segment.source_file)),
            "-vf", f"crop={width}:{height}:{x}:{y},fps={sample_fps},scale={sample_width}:{sample_height}:flags=bicubic,format=gray",
            "-f", "rawvideo", "-",
        ],
        cancel_event=cancel_event, resource_meter=resource_meter,
    )
    frame_size = sample_width * sample_height
    frames = [
        raw[offset:offset + frame_size]
        for offset in range(0, len(raw) - frame_size + 1, frame_size)
    ]
    scores = [_text_shape_score(frame, sample_width, sample_height) for frame in frames]
    bounds = [_text_shape_bounds(frame, sample_width, sample_height) for frame in frames]
    # 两帧确认 + 一帧滞回：避免单帧压缩噪声闪烁，但不把真正无字空档长期吞掉。
    active = [score >= 0.38 for score in scores]
    confirmed = [False] * len(active)
    for index, value in enumerate(active):
        if value and ((index > 0 and active[index - 1]) or (index + 1 < len(active) and active[index + 1])):
            confirmed[index] = True
            if index > 0 and active[index - 1]: confirmed[index - 1] = True
            if index + 1 < len(active) and active[index + 1]: confirmed[index + 1] = True
    candidates: list[CaptionTextCandidate] = []

    def append_candidate(frame_start: int, frame_end: int) -> None:
        """将一段稳定字幕框变成工程事件；宽度突变即视为换了一条字幕。"""

        source_in = segment.in_us + frame_start * MICROSECONDS // sample_fps
        source_out = min(segment.out_us, segment.in_us + frame_end * MICROSECONDS // sample_fps)
        if source_out <= source_in:
            return
        interval_bounds = [item for item in bounds[frame_start:frame_end] if item is not None]
        if interval_bounds:
            left = min(item[0] for item in interval_bounds)
            top = min(item[1] for item in interval_bounds)
            right = max(item[2] for item in interval_bounds)
            bottom = max(item[3] for item in interval_bounds)
        else:
            left, top, right, bottom = 0, 0, sample_width, sample_height
        box_x = max(0.0, float(x_ratio) + (left / sample_width) * float(width_ratio) - 0.012)
        box_y = max(0.0, float(y_ratio) + (top / sample_height) * float(height_ratio) - 0.010)
        box_right = min(1.0, float(x_ratio) + (right / sample_width) * float(width_ratio) + 0.012)
        box_bottom = min(1.0, float(y_ratio) + (bottom / sample_height) * float(height_ratio) + 0.010)
        candidates.append(CaptionTextCandidate(
            source_file=segment.source_file, source_in_us=source_in, source_out_us=source_out,
            confidence=round(max(scores[frame_start:frame_end], default=0.0), 3),
            x_ratio=round(box_x, 4), y_ratio=round(box_y, 4),
            width_ratio=round(max(0.01, box_right - box_x), 4),
            height_ratio=round(max(0.01, box_bottom - box_y), 4),
            evidence="ROI 文字形态边缘／行分布候选；非 OCR，需预览校准。",
        ))

    start: int | None = None
    for index, value in enumerate([*confirmed, False]):
        if value and start is None:
            start = index
        elif start is not None:
            piece_start = start
            previous = bounds[start]
            for split_index in range(start + 1, index):
                current = bounds[split_index]
                if previous and current:
                    previous_width = previous[2] - previous[0]
                    current_width = current[2] - current[0]
                    changed = (
                        abs(previous_width - current_width) >= sample_width * 0.13
                        or abs(previous[0] - current[0]) >= sample_width * 0.08
                    )
                    if changed and split_index - piece_start >= 3:
                        append_candidate(piece_start, split_index)
                        piece_start = split_index
                if current:
                    previous = current
            append_candidate(piece_start, index)
            start = None
    # 同一条硬字幕会因压缩、描边闪烁而在 6fps 采样中短暂断开。仅当位置和
    # 尺寸近似一致时合并，既减少工程事件，也不会把换行／换句误当成同一贴纸。
    coalesced: list[CaptionTextCandidate] = []
    for candidate in candidates:
        previous = coalesced[-1] if coalesced else None
        same_box = previous and (
            abs(previous.x_ratio - candidate.x_ratio) <= 0.035
            and abs(previous.y_ratio - candidate.y_ratio) <= 0.025
            and abs(previous.width_ratio - candidate.width_ratio) <= 0.055
            and abs(previous.height_ratio - candidate.height_ratio) <= 0.030
        )
        if same_box and candidate.source_in_us - previous.source_out_us <= 350_000:
            coalesced[-1] = replace(
                previous,
                source_out_us=candidate.source_out_us,
                confidence=max(previous.confidence, candidate.confidence),
            )
        else:
            coalesced.append(candidate)
    return CaptionTextDetectionResult(
        source_file=segment.source_file, analyzed_in_us=segment.in_us, analyzed_out_us=segment.out_us,
        sample_count=len(scores), candidates=tuple(coalesced),
        note="本地文字形态检测只判断选区是否有成行字形，不识别台词内容；亮物体或复杂特效仍可能误检。",
    )
