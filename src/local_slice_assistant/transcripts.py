"""与“本地音视频转写器”稳定 JSON 文件协议的只读适配。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .errors import ManifestValidationError
from .models import Cut
from .timeline import build_timeline


_SOURCE_LINE = re.compile(r"^(?:-\s*)?源文件[：:]\s*`?(?P<source>[^`]+?)`?\s*$")
_TXT_SOURCE_LINE = re.compile(r"^(?:第[^｜]+(?:集|个文件)|来源文件)｜(?P<source>.+)$")
_SRT_SOURCE = re.compile(r"^【(?:[^｜]+｜)?(?P<source>[^｜]+)｜完整时长 \d+:\d{2}:\d{2}(?:\.\d{1,3})?】$")
_SRT_SUMMARY = re.compile(r"^【《.*》｜音视频数量 \d+｜全部完整时长 \d+:\d{2}:\d{2}(?:\.\d{1,3})?｜以下时间码均为各自源文件原始时间】$")


def _read_transcript_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise ManifestValidationError("台词文件不是 UTF-8 编码，请另存为 UTF-8 后导入。", detail=str(path)) from exc
    except OSError as exc:
        raise ManifestValidationError("无法读取台词文件。", detail=str(path)) from exc


def transcript_needs_source_binding(path: str | Path) -> bool:
    """Never infer an episode from list order; absent declarations need a binding."""
    suffix = Path(path).suffix.casefold()
    if suffix == ".srt":
        return not any(_SRT_SOURCE.fullmatch(line.strip()) for line in _read_transcript_text(path).splitlines())
    if suffix in {".md", ".markdown", ".txt"}:
        return not any(_SOURCE_LINE.fullmatch(line.strip()) or _TXT_SOURCE_LINE.fullmatch(line.strip()) for line in _read_transcript_text(path).splitlines())
    return False


@dataclass(frozen=True, slots=True)
class TranscriptCue:
    source_file: str
    start_us: int
    end_us: int
    text: str
    source_kind: str = "transcript_candidate"


@dataclass(frozen=True, slots=True)
class TimelineCue:
    start_us: int
    end_us: int
    text: str
    source_file: str


def cue_key(cue: TranscriptCue) -> str:
    return f"{cue.source_file}|{cue.start_us}|{cue.end_us}"


def apply_overrides(
    cues: Iterable[TranscriptCue], overrides: dict[str, str]
) -> list[TranscriptCue]:
    return [
        TranscriptCue(
            source_file=cue.source_file,
            start_us=cue.start_us,
            end_us=cue.end_us,
            text=overrides.get(cue_key(cue), cue.text),
            source_kind=cue.source_kind,
        )
        for cue in cues
    ]


def _seconds_to_us(value: object) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a timestamp")
        return int(
            (Decimal(str(value)) * Decimal(1_000_000)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
    except Exception as exc:
        raise ManifestValidationError("转写 JSON 中存在无效的秒数。") from exc


def _timestamp_to_us(value: str) -> int:
    match = re.fullmatch(
        r"(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})(?:[,.](?P<millis>\d{1,3}))?",
        value.strip(),
    )
    if not match:
        raise ManifestValidationError("字幕文件包含无法识别的时间码。", detail=value)
    if int(match.group("minutes")) >= 60 or int(match.group("seconds")) >= 60:
        raise ManifestValidationError("时间码的分、秒必须小于 60。", detail=value)
    millis = (match.group("millis") or "0").ljust(3, "0")
    return (
        (int(match.group("hours")) * 3_600 + int(match.group("minutes")) * 60 + int(match.group("seconds")))
        * 1_000_000
        + int(millis) * 1_000
    )


def load_transcriber_json(path: str | Path) -> list[TranscriptCue]:
    try:
        raw = json.loads(_read_transcript_text(path))
    except json.JSONDecodeError as exc:
        raise ManifestValidationError("无法读取本地转写器导出的 JSON。", detail=str(exc)) from exc
    episodes = raw.get("episodes") if isinstance(raw, dict) else None
    if not isinstance(episodes, list):
        raise ManifestValidationError("转写 JSON 缺少 episodes 数组。")
    cues: list[TranscriptCue] = []
    for episode in episodes:
        if not isinstance(episode, dict) or not isinstance(episode.get("source_file"), str):
            raise ManifestValidationError("转写 JSON 的 episode 缺少 source_file。")
        source_file = episode["source_file"].replace("\\", "/")
        segments = episode.get("segments")
        if not isinstance(segments, list):
            raise ManifestValidationError("转写 JSON 的 episode 缺少 segments 数组。")
        for item in segments:
            if not isinstance(item, dict):
                raise ManifestValidationError("转写 JSON 的台词必须为对象。")
            start_us = _seconds_to_us(item.get("start_seconds"))
            end_us = _seconds_to_us(item.get("end_seconds"))
            if start_us < 0 or end_us <= start_us:
                raise ManifestValidationError("转写 JSON 的台词起止时间无效。", detail=source_file)
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ManifestValidationError("转写 JSON 存在空台词或非文本台词。", detail=source_file)
            cues.append(
                TranscriptCue(
                    source_file=source_file,
                    start_us=start_us,
                    end_us=end_us,
                    text=text.strip(),
                    source_kind="transcript_candidate",
                )
            )
    if not cues:
        raise ManifestValidationError("转写 JSON 没有可用的带时间戳台词。")
    return cues


def load_srt(path: str | Path, *, source_file: str | None = None) -> list[TranscriptCue]:
    text = _read_transcript_text(path)
    explicit_binding = source_file.replace("\\", "/") if source_file else None
    source_file = explicit_binding
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    cues: list[TranscriptCue] = []
    for block_number, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            raise ManifestValidationError("SRT 字幕块不完整，不能跳过后继续导入。", detail=f"{Path(path).name}：第 {block_number} 块")
        timestamp_index = 1 if re.fullmatch(r"\d+", lines[0]) else 0
        if timestamp_index >= len(lines) or "-->" not in lines[timestamp_index]:
            raise ManifestValidationError("SRT 字幕块缺少起止时间。", detail=f"{Path(path).name}：第 {block_number} 块")
        start_text, end_text = [
            item.strip() for item in lines[timestamp_index].split("-->", maxsplit=1)
        ]
        start_us = _timestamp_to_us(start_text)
        end_us = _timestamp_to_us(end_text)
        if end_us <= start_us or not lines[timestamp_index + 1 :]:
            raise ManifestValidationError("SRT 字幕时间无效或没有台词。", detail=f"{Path(path).name}：第 {block_number} 块")
        if any("-->" in line for line in lines[timestamp_index + 1 :]):
            raise ManifestValidationError("SRT 字幕块之间缺少空行。", detail=f"{Path(path).name}：第 {block_number} 块")
        cue_text = " ".join(lines[timestamp_index + 1 :])
        if start_us == 0 and end_us == 1000:
            source_match = _SRT_SOURCE.fullmatch(cue_text)
            if source_match:
                source_file = source_match.group("source").replace("\\", "/")
                if explicit_binding and source_file != explicit_binding:
                    raise ManifestValidationError("台词声明的源文件与手动绑定不同，请核对。")
                continue
            if _SRT_SUMMARY.fullmatch(cue_text):
                continue
        if not source_file:
            raise ManifestValidationError("导入 SRT 时必须选择对应源素材，或保留各集源文件说明条目。")
        cues.append(
            TranscriptCue(
                source_file=source_file.replace("\\", "/"),
                start_us=start_us,
                end_us=end_us,
                text=cue_text,
                source_kind="srt",
            )
        )
    if not cues:
        raise ManifestValidationError("SRT 没有可用的带时间戳台词。")
    return cues


def load_transcriber_markdown(path: str | Path, *, source_file: str | None = None) -> list[TranscriptCue]:
    lines = _read_transcript_text(path).splitlines()
    explicit_binding = source_file.replace("\\", "/") if source_file else None
    source_file = explicit_binding
    cues: list[TranscriptCue] = []
    cue_pattern = re.compile(
        r"^(?:-\s*)?(?:\*\*|\[)?(?P<start>\d+:\d{2}:\d{2}(?:[.,]\d{1,3})?)"
        r"\s*(?:-->|–|—|-|→)\s*(?P<end>\d+:\d{2}:\d{2}(?:[.,]\d{1,3})?)"
        r"(?:\*\*|\])?\s+(?P<text>.+)$"
    )
    for line_number, line in enumerate(lines, start=1):
        line = line.strip()
        source_match = _SOURCE_LINE.match(line) or _TXT_SOURCE_LINE.fullmatch(line)
        if source_match:
            source_file = source_match.group("source").replace("\\", "/")
            if explicit_binding and source_file != explicit_binding:
                raise ManifestValidationError("台词声明的源文件与手动绑定不同，请核对。", detail=f"{Path(path).name}：第 {line_number} 行")
            continue
        cue_match = cue_pattern.match(line)
        if not cue_match:
            # Reading exports contain document metadata and a media inventory.
            # Their dates/durations are not dialogue; keep malformed cue lines strict.
            if re.match(
                r"^(?:-\s*)?(?:生成时间|全部音视频总时长|时间码说明|完整时长)[：:]",
                line,
            ) or re.fullmatch(
                r"\|\s*\d+\s*\|[^|]*\|\s*`[^`]+`\s*\|\s*\d+:\d{2}:\d{2}(?:[.,]\d{1,3})?\s*\|[^|]*\|",
                line,
            ):
                continue
            if re.search(r"\d+:\d{2}:\d{2}", line):
                raise ManifestValidationError("台词行格式不完整：需要开始时间、结束时间和同一行台词。", detail=f"{Path(path).name}：第 {line_number} 行：{line}")
            continue
        if not source_file:
            raise ManifestValidationError("请在台词前填写“源文件：视频文件名.mp4”。")
        start_us = _timestamp_to_us(cue_match.group("start"))
        end_us = _timestamp_to_us(cue_match.group("end"))
        if end_us <= start_us:
            raise ManifestValidationError("台词结束时间必须晚于开始时间。", detail=line)
        if end_us > start_us:
            cues.append(
                TranscriptCue(
                    source_file=source_file,
                    start_us=start_us,
                    end_us=end_us,
                    text=cue_match.group("text").strip(),
                    source_kind="transcript_candidate",
                )
            )
    if not source_file:
        raise ManifestValidationError(
            "Markdown 台词文件缺少“源文件”行，无法绑定到素材。"
        )
    if not cues:
        raise ManifestValidationError("没有读取到台词；请使用“00:00:01.000 --> 00:00:02.000 台词”格式。")
    return cues


def load_transcript(
    path: str | Path, *, source_file: str | None = None
) -> list[TranscriptCue]:
    suffix = Path(path).suffix.casefold()
    if suffix == ".json":
        return load_transcriber_json(path)
    if suffix == ".srt":
        return load_srt(path, source_file=source_file)
    if suffix in {".md", ".markdown", ".txt"}:
        return load_transcriber_markdown(path, source_file=source_file)
    raise ManifestValidationError("台词文件仅支持 JSON、MD、TXT 或 SRT。")


def load_transcript_files(
    paths: Iterable[str],
    overrides: dict[str, str],
    *,
    source_bindings: dict[str, str] | None = None,
) -> list[TranscriptCue]:
    """读取工程登记的台词文件。

    转写器 JSON / Markdown 自带 source_file；单集 SRT 则使用工程中明确保存的
    文件到素材相对路径绑定，绝不凭文件排序猜测剧集。
    """

    bindings = source_bindings or {}
    all_cues: list[TranscriptCue] = []
    for item in paths:
        item_path = Path(item)
        binding = bindings.get(str(item_path))
        if binding is None:
            try:
                binding = bindings.get(str(item_path.resolve()))
            except OSError:
                binding = None
        all_cues.extend(load_transcript(item_path, source_file=binding))
    # Importing both the SRT and MD export of one episode must not double its
    # evidence or rendered subtitles. Conflicting text at one anchor is surfaced.
    unique: dict[str, TranscriptCue] = {}
    for cue in apply_overrides(all_cues, overrides):
        key = cue_key(cue)
        existing = unique.get(key)
        if existing and existing.text != cue.text:
            raise ManifestValidationError("同一素材、同一时间有不同台词，请只导入核对后的版本。", detail=key)
        if existing is None or cue.source_kind == "srt":
            unique[key] = cue
    return list(unique.values())


def map_cues_to_timeline(
    cut: Cut, cues: Iterable[TranscriptCue]
) -> list[TimelineCue]:
    """裁切或重排后，按每段源时间交集重新映射到输出时间轴。"""

    by_source: dict[str, list[TranscriptCue]] = {}
    for cue in cues:
        by_source.setdefault(cue.source_file, []).append(cue)
    mapped: list[TimelineCue] = []
    for placement in build_timeline(cut):
        segment = placement.segment
        for cue in by_source.get(segment.source_file, []):
            overlap_start = max(cue.start_us, segment.in_us)
            overlap_end = min(cue.end_us, segment.out_us)
            if overlap_end <= overlap_start:
                continue
            mapped.append(
                TimelineCue(
                    start_us=placement.output_in_us
                    + segment.source_offset_to_output_us(
                        overlap_start - segment.in_us
                    ),
                    end_us=placement.output_in_us
                    + segment.source_offset_to_output_us(
                        overlap_end - segment.in_us
                    ),
                    text=cue.text,
                    source_file=cue.source_file,
                )
            )
    return mapped
