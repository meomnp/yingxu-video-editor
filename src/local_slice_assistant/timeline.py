"""串联时间轴计算与字幕映射共用的纯函数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import Cut, Segment
from .packaging import title_cards_for_cut


@dataclass(frozen=True, slots=True)
class SegmentPlacement:
    segment: Segment
    output_in_us: int
    output_out_us: int


@dataclass(frozen=True, slots=True)
class RenderPlacement:
    """成片渲染顺序中的一个源片段或字卡。"""

    kind: str
    output_in_us: int
    output_out_us: int
    segment: Segment | None = None
    title_card: dict[str, Any] | None = None


def build_render_timeline(cut: Cut) -> list[RenderPlacement]:
    """计算包含字卡的渲染顺序；原片段身份和源时间不被字卡替代。"""

    cards = title_cards_for_cut(cut)
    cursor = 0
    placements: list[RenderPlacement] = []

    def add_card(card: dict[str, Any]) -> None:
        nonlocal cursor
        duration = int(card["duration_us"])
        placements.append(
            RenderPlacement(
                kind="title_card",
                output_in_us=cursor,
                output_out_us=cursor + duration,
                title_card=card,
            )
        )
        cursor += duration

    for segment in cut.segments:
        for card in cards:
            if card.get("position", "after") == "before" and card.get(
                "anchor_segment_id"
            ) == segment.id:
                add_card(card)
        placements.append(
            RenderPlacement(
                kind="segment",
                output_in_us=cursor,
                output_out_us=cursor + segment.duration_us,
                segment=segment,
            )
        )
        cursor += segment.duration_us
        for card in cards:
            if card.get("position", "after") == "after" and card.get(
                "anchor_segment_id"
            ) == segment.id:
                add_card(card)
    for card in cards:
        if card.get("position", "after") == "end":
            add_card(card)
    return placements


def build_timeline(cut: Cut) -> list[SegmentPlacement]:
    return [
        SegmentPlacement(
            segment=placement.segment,
            output_in_us=placement.output_in_us,
            output_out_us=placement.output_out_us,
        )
        for placement in build_render_timeline(cut)
        if placement.segment is not None
    ]


def total_duration_us(cut: Cut) -> int:
    placements = build_render_timeline(cut)
    return placements[-1].output_out_us if placements else 0


def format_timecode_us(value_us: int) -> str:
    sign = "-" if value_us < 0 else ""
    milliseconds = abs(value_us) // 1_000
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1_000)
    return f"{sign}{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"
