"""第二阶段的局部、确定性接缝候选。

候选只提供台词边界、声音活动和低分辨率镜头变化证据。它们从不自动删除、
重排或改写工程，所有采用动作仍由用户显式确认并进入可撤销命令历史。
"""

from __future__ import annotations

from array import array
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from threading import Event
from typing import Any, Iterable
from uuid import uuid4

from .analysis_cache import AnalysisCache
from .errors import AnalysisCancelled, MediaProbeError, ManifestValidationError
from .ffmpeg import ffmpeg_binary, ffprobe_binary
from .models import ProjectDocument, Segment, SourceInfo
from .paths import resolve_excluded_dirs, resolve_media_root, safe_resolve_media_path
from .resources import ResourceMeter, ResourcePolicy
from .transcripts import TranscriptCue, apply_overrides, cue_key


MICROSECONDS = 1_000_000
DEFAULT_WINDOW_US = MICROSECONDS
DEFAULT_MAX_CANDIDATE_OFFSET_US = MICROSECONDS


@dataclass(frozen=True, slots=True)
class AudioBucket:
    start_us: int
    end_us: int
    rms: float
    active: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_us": self.start_us,
            "end_us": self.end_us,
            "rms": self.rms,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AudioBucket":
        return cls(
            start_us=int(raw["start_us"]),
            end_us=int(raw["end_us"]),
            rms=float(raw["rms"]),
            active=bool(raw["active"]),
        )


@dataclass(frozen=True, slots=True)
class JunctionCandidate:
    id: str
    side: str
    segment_id: str
    source_file: str
    current_us: int
    proposed_us: int
    kind: str
    reason: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "side": self.side,
            "segment_id": self.segment_id,
            "source_file": self.source_file,
            "current_us": self.current_us,
            "proposed_us": self.proposed_us,
            "kind": self.kind,
            "reason": self.reason,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JunctionCandidate":
        return cls(
            id=str(raw["id"]),
            side=str(raw["side"]),
            segment_id=str(raw["segment_id"]),
            source_file=str(raw["source_file"]),
            current_us=int(raw["current_us"]),
            proposed_us=int(raw["proposed_us"]),
            kind=str(raw["kind"]),
            reason=str(raw["reason"]),
            evidence=str(raw["evidence"]),
        )


@dataclass(frozen=True, slots=True)
class JunctionAnalysis:
    revision: int
    cut_id: str
    junction_index: int
    left_segment_id: str
    right_segment_id: str
    left_cues: tuple[TranscriptCue, ...] = ()
    right_cues: tuple[TranscriptCue, ...] = ()
    left_waveform: tuple[AudioBucket, ...] = ()
    right_waveform: tuple[AudioBucket, ...] = ()
    candidates: tuple[JunctionCandidate, ...] = ()
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "cut_id": self.cut_id,
            "junction_index": self.junction_index,
            "left_segment_id": self.left_segment_id,
            "right_segment_id": self.right_segment_id,
            "left_cues": [
                {
                    "source_file": cue.source_file,
                    "start_us": cue.start_us,
                    "end_us": cue.end_us,
                    "text": cue.text,
                }
                for cue in self.left_cues
            ],
            "right_cues": [
                {
                    "source_file": cue.source_file,
                    "start_us": cue.start_us,
                    "end_us": cue.end_us,
                    "text": cue.text,
                }
                for cue in self.right_cues
            ],
            "left_waveform": [item.to_dict() for item in self.left_waveform],
            "right_waveform": [item.to_dict() for item in self.right_waveform],
            "candidates": [item.to_dict() for item in self.candidates],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, from_cache: bool = False) -> "JunctionAnalysis":
        def cue_from(value: dict[str, Any]) -> TranscriptCue:
            return TranscriptCue(
                source_file=str(value["source_file"]),
                start_us=int(value["start_us"]),
                end_us=int(value["end_us"]),
                text=str(value["text"]),
            )

        return cls(
            revision=int(raw["revision"]),
            cut_id=str(raw["cut_id"]),
            junction_index=int(raw["junction_index"]),
            left_segment_id=str(raw["left_segment_id"]),
            right_segment_id=str(raw["right_segment_id"]),
            left_cues=tuple(cue_from(item) for item in raw.get("left_cues", [])),
            right_cues=tuple(cue_from(item) for item in raw.get("right_cues", [])),
            left_waveform=tuple(
                AudioBucket.from_dict(item) for item in raw.get("left_waveform", [])
            ),
            right_waveform=tuple(
                AudioBucket.from_dict(item) for item in raw.get("right_waveform", [])
            ),
            candidates=tuple(
                JunctionCandidate.from_dict(item) for item in raw.get("candidates", [])
            ),
            from_cache=from_cache,
        )


def _seconds(value_us: int) -> str:
    return f"{value_us / MICROSECONDS:.6f}"


def _decimal_to_us(value: object) -> int:
    return int(
        (Decimal(str(value)) * Decimal(MICROSECONDS)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


def _source_path(document: ProjectDocument, source_file: str) -> Path:
    root = resolve_media_root(document.media_root)
    _relative, path = safe_resolve_media_path(
        root, source_file, exclusions=resolve_excluded_dirs(root)
    )
    return path


def _stop_child(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _capture_process(
    command: list[str],
    *,
    timeout: int,
    cancel_event: Event | None,
    failure_message: str,
    resource_meter: ResourceMeter | None = None,
) -> bytes:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise MediaProbeError(failure_message, detail=str(exc)) from exc
    started = time.monotonic()
    poll_interval = 0.05 if resource_meter else 0.2
    while True:
        if resource_meter:
            resource_meter.observe(process.pid)
        if cancel_event and cancel_event.is_set():
            _stop_child(process)
            raise AnalysisCancelled("基础分析已取消，已停止本工具启动的 FFmpeg。")
        if time.monotonic() - started > timeout:
            _stop_child(process)
            raise MediaProbeError(failure_message, detail="局部读取超过 90 秒。")
        try:
            stdout, stderr = process.communicate(timeout=poll_interval)
            break
        except subprocess.TimeoutExpired:
            continue
    if resource_meter:
        resource_meter.observe(process.pid)
    if process.returncode:
        raise MediaProbeError(
            failure_message,
            detail=stderr.decode("utf-8", errors="replace")[-1200:],
        )
    return stdout


def _capture_ffmpeg(
    arguments: list[str],
    *,
    timeout: int = 90,
    cancel_event: Event | None = None,
    resource_meter: ResourceMeter | None = None,
) -> bytes:
    return _capture_process(
        [ffmpeg_binary(), "-hide_banner", "-nostdin", "-v", "error", *arguments],
        timeout=timeout,
        cancel_event=cancel_event,
        failure_message="本地基础分析无法读取所选接缝附近的媒体。",
        resource_meter=resource_meter,
    )


def audio_buckets(
    path: str | Path,
    *,
    start_us: int,
    end_us: int,
    has_audio: bool,
    bucket_ms: int = 100,
    cpu_threads: int = 6,
    cancel_event: Event | None = None,
    resource_meter: ResourceMeter | None = None,
) -> list[AudioBucket]:
    """仅解码指定局部窗口为低采样率单声道，不读取整集。"""

    if not has_audio or end_us <= start_us:
        return []
    sample_rate = 8_000
    raw = _capture_ffmpeg(
        [
            "-threads",
            str(max(1, cpu_threads)),
            "-i",
            os.fspath(path),
            "-ss",
            _seconds(start_us),
            "-t",
            _seconds(end_us - start_us),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-",
        ],
        cancel_event=cancel_event,
        resource_meter=resource_meter,
    )
    samples = array("h")
    samples.frombytes(raw)
    if not samples:
        return []
    samples_per_bucket = max(1, sample_rate * bucket_ms // 1_000)
    buckets: list[AudioBucket] = []
    for offset in range(0, len(samples), samples_per_bucket):
        portion = samples[offset : offset + samples_per_bucket]
        if not portion:
            continue
        rms = math.sqrt(sum(value * value for value in portion) / len(portion)) / 32768
        bucket_start = start_us + offset * MICROSECONDS // sample_rate
        bucket_end = min(
            end_us,
            start_us + (offset + len(portion)) * MICROSECONDS // sample_rate,
        )
        buckets.append(
            AudioBucket(
                start_us=bucket_start,
                end_us=bucket_end,
                rms=rms,
                active=rms >= 0.015,
            )
        )
    return buckets


def scene_change_times(
    path: str | Path,
    source: SourceInfo,
    *,
    start_us: int,
    end_us: int,
    cpu_threads: int = 6,
    cancel_event: Event | None = None,
    resource_meter: ResourceMeter | None = None,
) -> list[tuple[int, float]]:
    """低分辨率亮度差候选；它不是视觉模型，也不解释剧情。"""

    if end_us <= start_us or source.width <= 0 or source.height <= 0:
        return []
    width = min(160, source.width)
    width -= width % 2
    height = max(2, round(source.height * width / source.width))
    height -= height % 2
    sample_fps = 4
    raw = _capture_ffmpeg(
        [
            "-threads",
            str(max(1, cpu_threads)),
            "-i",
            os.fspath(path),
            "-ss",
            _seconds(start_us),
            "-t",
            _seconds(end_us - start_us),
            "-an",
            "-vf",
            f"fps={sample_fps},scale={width}:{height},format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        cancel_event=cancel_event,
        resource_meter=resource_meter,
    )
    frame_size = width * height
    if frame_size <= 0:
        return []
    frames = [
        raw[offset : offset + frame_size]
        for offset in range(0, len(raw) - frame_size + 1, frame_size)
    ]
    results: list[tuple[int, float]] = []
    for index in range(1, len(frames)):
        previous, current = frames[index - 1], frames[index]
        score = sum(abs(left - right) for left, right in zip(previous, current)) / (
            frame_size * 255
        )
        if score >= 0.16:
            results.append((start_us + index * MICROSECONDS // sample_fps, score))
    return results


def frame_times_near(
    path: str | Path,
    *,
    center_us: int,
    radius_us: int = 2 * MICROSECONDS,
    cancel_event: Event | None = None,
    resource_meter: ResourceMeter | None = None,
) -> list[int]:
    """读取 ffprobe 的实际帧 PTS，适用于 VFR 的逐帧微调。"""

    start_us = max(0, center_us - radius_us)
    duration_us = radius_us * 2
    command = [
        ffprobe_binary(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-read_intervals",
        f"{_seconds(start_us)}%+{_seconds(duration_us)}",
        "-show_entries",
        "frame=best_effort_timestamp_time",
        "-show_frames",
        "-of",
        "json",
        os.fspath(path),
    ]
    try:
        raw = json.loads(
            _capture_process(
                command,
                timeout=90,
                cancel_event=cancel_event,
                failure_message="ffprobe 无法读取真实视频帧时间。",
                resource_meter=resource_meter,
            ).decode("utf-8", errors="replace")
        )
    except json.JSONDecodeError as exc:
        raise MediaProbeError("ffprobe 帧时间输出无效。") from exc
    values = {
        _decimal_to_us(item["best_effort_timestamp_time"])
        for item in raw.get("frames", [])
        if item.get("best_effort_timestamp_time") not in {None, "N/A"}
    }
    return sorted(values)


def neighboring_frame_time(
    document: ProjectDocument,
    segment: Segment,
    *,
    boundary: str,
    direction: int,
    cancel_event: Event | None = None,
    resource_meter: ResourceMeter | None = None,
) -> int:
    if boundary not in {"in", "out"} or direction not in {-1, 1}:
        raise ManifestValidationError("逐帧调整参数无效。")
    current = segment.in_us if boundary == "in" else segment.out_us
    frames = frame_times_near(
        _source_path(document, segment.source_file),
        center_us=current,
        cancel_event=cancel_event,
        resource_meter=resource_meter,
    )
    epsilon = 1
    if direction < 0:
        choices = [item for item in frames if item < current - epsilon]
        if not choices:
            raise ManifestValidationError("当前局部范围内没有更早的真实视频帧。")
        return max(choices)
    choices = [item for item in frames if item > current + epsilon]
    if not choices:
        raise ManifestValidationError("当前局部范围内没有更晚的真实视频帧。")
    return min(choices)


def _relevant_cues(
    cues: Iterable[TranscriptCue], source_file: str, start_us: int, end_us: int
) -> list[TranscriptCue]:
    return [
        cue
        for cue in cues
        if cue.source_file == source_file
        and cue.end_us >= start_us
        and cue.start_us <= end_us
    ]


def _candidate(
    *,
    side: str,
    segment: Segment,
    current_us: int,
    proposed_us: int,
    kind: str,
    reason: str,
    evidence: str,
) -> JunctionCandidate | None:
    if (
        proposed_us < segment.in_us
        or proposed_us > segment.out_us
        or abs(proposed_us - current_us) > DEFAULT_MAX_CANDIDATE_OFFSET_US
    ):
        return None
    return JunctionCandidate(
        id=uuid4().hex,
        side=side,
        segment_id=segment.id,
        source_file=segment.source_file,
        current_us=current_us,
        proposed_us=proposed_us,
        kind=kind,
        reason=reason,
        evidence=evidence,
    )


def _unique_candidates(items: Iterable[JunctionCandidate | None]) -> tuple[JunctionCandidate, ...]:
    seen: set[tuple[str, int, str]] = set()
    output: list[JunctionCandidate] = []
    for item in items:
        if not item:
            continue
        key = (item.side, item.proposed_us, item.kind)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return tuple(sorted(output, key=lambda item: (item.side, item.proposed_us, item.kind)))


def analyze_junction(
    document: ProjectDocument,
    *,
    cut_id: str,
    junction_index: int,
    transcript_cues: Iterable[TranscriptCue] = (),
    policy: ResourcePolicy,
    cache: AnalysisCache | None = None,
    cancel_event: Event | None = None,
    resource_meter: ResourceMeter | None = None,
) -> JunctionAnalysis:
    """生成当前工程修订号专属的建议；迟到结果由 GUI 按 revision 丢弃。"""

    if cancel_event and cancel_event.is_set():
        raise AnalysisCancelled("基础分析已取消。")
    cut = document.get_cut(cut_id)
    if not 0 <= junction_index < len(cut.segments) - 1:
        raise ManifestValidationError("请选择有效的相邻片段接缝。")
    left, right = cut.segments[junction_index], cut.segments[junction_index + 1]
    supplied_cues = apply_overrides(transcript_cues, document.transcript_overrides)
    left_start = max(left.in_us, left.out_us - DEFAULT_WINDOW_US)
    right_end = min(right.out_us, right.in_us + DEFAULT_WINDOW_US)
    cache_payload = {
        "version": 1,
        "revision": document.revision,
        "cut_id": cut_id,
        "junction_index": junction_index,
        "left": [left.id, left.source_file, left.in_us, left.out_us],
        "right": [right.id, right.source_file, right.in_us, right.out_us],
        "source_hashes": {
            left.source_file: document.source_for(left.source_file).quick_hash,
            right.source_file: document.source_for(right.source_file).quick_hash,
        },
        "cue_keys": [cue_key(cue) + "|" + cue.text for cue in supplied_cues],
        "mode": policy.mode,
    }
    key = AnalysisCache.key_for(cache_payload)
    if cache:
        cached = cache.get(key)
        if cached:
            return JunctionAnalysis.from_dict(cached, from_cache=True)

    left_source = document.source_for(left.source_file)
    right_source = document.source_for(right.source_file)
    left_path = _source_path(document, left.source_file)
    right_path = _source_path(document, right.source_file)
    left_cues = _relevant_cues(supplied_cues, left.source_file, left_start, left.out_us)
    right_cues = _relevant_cues(supplied_cues, right.source_file, right.in_us, right_end)
    left_waveform = audio_buckets(
        left_path,
        start_us=left_start,
        end_us=left.out_us,
        has_audio=left_source.has_audio,
        cpu_threads=policy.cpu_threads,
        cancel_event=cancel_event,
        resource_meter=resource_meter,
    )
    right_waveform = audio_buckets(
        right_path,
        start_us=right.in_us,
        end_us=right_end,
        has_audio=right_source.has_audio,
        cpu_threads=policy.cpu_threads,
        cancel_event=cancel_event,
        resource_meter=resource_meter,
    )
    left_scenes = scene_change_times(
        left_path,
        left_source,
        start_us=left_start,
        end_us=left.out_us,
        cpu_threads=policy.cpu_threads,
        cancel_event=cancel_event,
        resource_meter=resource_meter,
    )
    right_scenes = scene_change_times(
        right_path,
        right_source,
        start_us=right.in_us,
        end_us=right_end,
        cpu_threads=policy.cpu_threads,
        cancel_event=cancel_event,
        resource_meter=resource_meter,
    )

    candidates: list[JunctionCandidate | None] = []
    for cue in left_cues:
        candidates.append(
            _candidate(
                side="left_out",
                segment=left,
                current_us=left.out_us,
                proposed_us=cue.end_us,
                kind="transcript",
                reason="台词句尾候选",
                evidence=cue.text[:80] or "台词结束",
            )
        )
    for cue in right_cues:
        candidates.append(
            _candidate(
                side="right_in",
                segment=right,
                current_us=right.in_us,
                proposed_us=cue.start_us,
                kind="transcript",
                reason="台词句首候选",
                evidence=cue.text[:80] or "台词开始",
            )
        )
    for buckets, segment, current, side in (
        (left_waveform, left, left.out_us, "left_out"),
        (right_waveform, right, right.in_us, "right_in"),
    ):
        for previous, following in zip(buckets, buckets[1:]):
            if previous.active != following.active:
                candidates.append(
                    _candidate(
                        side=side,
                        segment=segment,
                        current_us=current,
                        proposed_us=following.start_us,
                        kind="audio",
                        reason=(
                            "声音活动结束候选"
                            if previous.active
                            else "声音活动开始候选"
                        ),
                        evidence="仅作声音证据；静音、反应和喜剧停顿不会自动删除。",
                    )
                )
    for scenes, segment, current, side in (
        (left_scenes, left, left.out_us, "left_out"),
        (right_scenes, right, right.in_us, "right_in"),
    ):
        for timestamp_us, score in scenes:
            candidates.append(
                _candidate(
                    side=side,
                    segment=segment,
                    current_us=current,
                    proposed_us=timestamp_us,
                    kind="scene",
                    reason="画面变化候选",
                    evidence=f"低分辨率亮度差 {score:.2f}；需结合台词和预览确认。",
                )
            )
    result = JunctionAnalysis(
        revision=document.revision,
        cut_id=cut_id,
        junction_index=junction_index,
        left_segment_id=left.id,
        right_segment_id=right.id,
        left_cues=tuple(left_cues),
        right_cues=tuple(right_cues),
        left_waveform=tuple(left_waveform),
        right_waveform=tuple(right_waveform),
        candidates=_unique_candidates(candidates),
    )
    if cache:
        cache.put(key, result.to_dict())
    return result
