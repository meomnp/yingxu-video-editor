"""Pure, offline safety rules for the Jianying screen-coordinate prototype.

This module deliberately has no Qt or Win32 imports.  It can be tested without
capturing a screen or sending desktop input.  The GUI adapter owns all platform
effects and must only call it after a user has explicitly started an attempt.
"""
from __future__ import annotations

from dataclasses import dataclass
import ntpath
from typing import Callable, Iterable, Sequence


RGB = tuple[int, int, int]
PixelAt = Callable[[int, int], RGB]


class MacroValidationError(ValueError):
    """A selected coordinate or screen configuration is unsafe to use."""


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class Gap:
    """A half-open x interval in screenshot pixels."""

    start: int
    end: int

    @property
    def width(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class DetectionPoints:
    caption: Point
    sticker: Point
    ruler: Point
    range_start: Point
    range_end: Point

    @property
    def left(self) -> int:
        return min(self.range_start.x, self.range_end.x)

    @property
    def right(self) -> int:
        return max(self.range_start.x, self.range_end.x)


@dataclass(frozen=True)
class DpiMapping:
    """Maps selector logical coordinates -> screenshot -> Win32 screen pixels.

    The prototype supports exactly one primary display.  ``screen_*`` are the
    physical dimensions reported by Win32, while ``image_*`` are screenshot
    pixels and ``logical_*`` are Qt selector/widget coordinates.
    """

    logical_width: int
    logical_height: int
    image_width: int
    image_height: int
    screen_width: int
    screen_height: int

    @classmethod
    def create(
        cls,
        logical_width: int,
        logical_height: int,
        image_width: int,
        image_height: int,
        screen_width: int,
        screen_height: int,
    ) -> "DpiMapping":
        values = (logical_width, logical_height, image_width, image_height, screen_width, screen_height)
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 2 for value in values):
            raise MacroValidationError("显示器或截图尺寸无效，已拒绝操作。")
        cls._require_matching_aspect(logical_width, logical_height, image_width, image_height, "Qt 选择层与截图")
        cls._require_matching_aspect(image_width, image_height, screen_width, screen_height, "截图与 Windows 桌面")
        return cls(*values)

    @staticmethod
    def _require_matching_aspect(width_a: int, height_a: int, width_b: int, height_b: int, label: str) -> None:
        # A tiny tolerance permits harmless integer rounding but rejects a
        # captured virtual desktop or a display that changed scale/rotation.
        ratio_difference = abs(width_a / height_a - width_b / height_b)
        if ratio_difference > 0.003:
            raise MacroValidationError(f"{label}比例不一致，DPI 映射不可信，已拒绝操作。")

    @staticmethod
    def _map_axis(value: int, source_extent: int, target_extent: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < source_extent:
            raise MacroValidationError("选点不在当前屏幕范围内。")
        return round(value * (target_extent - 1) / (source_extent - 1))

    def logical_to_image(self, point: Point) -> Point:
        return Point(
            self._map_axis(point.x, self.logical_width, self.image_width),
            self._map_axis(point.y, self.logical_height, self.image_height),
        )

    def image_to_screen(self, point: Point) -> Point:
        return Point(
            self._map_axis(point.x, self.image_width, self.screen_width),
            self._map_axis(point.y, self.image_height, self.screen_height),
        )


def validate_detection_points(
    points: Sequence[Point], image_width: int, image_height: int, *, min_range_width: int = 30
) -> DetectionPoints:
    """Reject incomplete, overlapping-track, or out-of-bounds user input."""
    if len(points) != 5:
        raise MacroValidationError("必须依次选择字幕、贴纸、时间尺和两个范围边界。")
    if min_range_width < 1:
        raise MacroValidationError("最小检测宽度无效。")
    for point in points:
        if not isinstance(point, Point) or not 0 <= point.x < image_width or not 0 <= point.y < image_height:
            raise MacroValidationError("选点超出截图范围，已停止检测。")
    result = DetectionPoints(*points)
    track_rows = (result.caption.y, result.sticker.y, result.ruler.y)
    if any(abs(first - second) < 6 for index, first in enumerate(track_rows) for second in track_rows[index + 1 :]):
        raise MacroValidationError("字幕、贴纸和时间尺必须是三条不同的行，已停止检测。")
    if result.right - result.left < min_range_width:
        raise MacroValidationError(f"处理范围至少需要 {min_range_width} 个截图像素。")
    return result


def is_caption_red(color: RGB) -> bool:
    red, green, blue = color
    return red > 85 and red > green * 1.3 and red > blue * 1.3


def is_orange_sticker(color: RGB) -> bool:
    red, green, blue = color
    return red > 110 and green > 65 and blue < 100 and red > green * 1.12 and green > blue * 1.15


def sticker_row_matches(before: PixelAt, after: PixelAt, start: int, end: int, y: int) -> bool:
    """Check sticker occupancy after Undo, ignoring a few border pixels."""
    if end <= start:
        return False
    mismatches = 0
    for x in range(start,end):
        old = sum(is_orange_sticker(before(x,row)) for row in (y-2,y,y+2)) >= 2
        new = sum(is_orange_sticker(after(x,row)) for row in (y-2,y,y+2)) >= 2
        mismatches += old != new
    return mismatches <= max(2,(end-start)//20)


def caption_runs(
    pixel_at: PixelAt, image_width: int, image_height: int, x0: int, x1: int, y: int, *, min_width: int = 3,
    row_radius: int = 2, min_votes: int = 3
) -> list[tuple[int, int]]:
    """Find red caption blocks by a five-row majority vote, without OCR."""
    if not 0 <= x0 < x1 <= image_width or not 0 <= y < image_height or min_width < 1 or row_radius < 0 or not 1 <= min_votes <= 2 * row_radius + 1:
        raise MacroValidationError("字幕检测坐标无效。")
    active: list[bool] = []
    for x in range(x0, x1):
        votes = sum(
            is_caption_red(pixel_at(x, row))
            for row in range(max(0, y - row_radius), min(image_height, y + row_radius + 1))
        )
        active.append(votes >= min_votes)
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate([*active, False]):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= min_width:
                runs.append((x0 + start, x0 + index))
            start = None
    return runs


def gaps_between(runs: Sequence[tuple[int, int]], *, margin: int = 2, min_gap_width: int = 10) -> list[Gap]:
    """Return only internal, sufficiently wide gaps; never infer end gaps."""
    if margin < 0 or min_gap_width < 1:
        raise MacroValidationError("间隙安全参数无效。")
    result: list[Gap] = []
    for left, right in zip(runs, runs[1:]):
        if left[0] >= left[1] or right[0] >= right[1] or right[0] < left[1]:
            raise MacroValidationError("字幕块顺序无效，已停止检测。")
        start, end = left[1] + margin, right[0] - margin
        if end - start >= min_gap_width:
            result.append(Gap(start, end))
    return result


def gap_has_continuous_sticker(pixel_at: PixelAt, gap: Gap, sticker_y: int) -> bool:
    """A gap is actionable only when every column is the chosen orange strip."""
    return gap.width > 0 and all(is_orange_sticker(pixel_at(x, sticker_y)) for x in range(gap.start, gap.end))


def safe_sticker_gaps(pixel_at: PixelAt, gaps: Iterable[Gap], sticker_y: int) -> list[Gap]:
    return [gap for gap in gaps if gap_has_continuous_sticker(pixel_at, gap, sticker_y)]


def remaining_sticker_gaps(pixel_at: PixelAt, gaps: Iterable[Gap], sticker_y: int) -> list[Gap]:
    """Allow existing cut borders/labels; skip gaps already mostly empty.

    These are detection candidates only: deletion still requires selection-edge
    verification by the desktop adapter.
    """
    result = []
    for gap in gaps:
        if gap.width < 10:
            continue
        middle = (gap.start + gap.end) // 2
        orange = sum(is_orange_sticker(pixel_at(x, sticker_y)) for x in range(gap.start, gap.end))
        if orange / gap.width >= 0.6 and any(is_orange_sticker(pixel_at(x, sticker_y)) for x in range(middle - 1, middle + 2)):
            result.append(gap)
    return result


def snapshot_still_matches(
    baseline: PixelAt, current: PixelAt, gap: Gap, sticker_y: int, *, tolerance: int = 18
) -> bool:
    """Fail closed if the planned sticker band has moved or changed materially."""
    if tolerance < 0 or gap.width <= 0:
        return False
    sample_count = min(11, gap.width)
    positions = {
        gap.start + round(index * (gap.width - 1) / max(1, sample_count - 1))
        for index in range(sample_count)
    }
    for x in positions:
        before, now = baseline(x, sticker_y), current(x, sticker_y)
        if any(abs(left - right) > tolerance for left, right in zip(before, now)):
            return False
    return any(is_orange_sticker(baseline(x, sticker_y)) for x in positions)


def is_jianying_executable(executable_path: str) -> bool:
    """Only the foreground JianyingPro.exe process is accepted; titles are ignored."""
    if not executable_path:
        return False
    return ntpath.basename(executable_path.strip().strip('"')).casefold() == "jianyingpro.exe"


def deletion_evidence(before: PixelAt, after: PixelAt, gap: Gap, y: int) -> dict:
    """Ignore at most 3 boundary pixels; require disappearance in the interior."""
    xs = list(range(gap.start + 3, gap.end - 3))
    if len(xs) < 4:
        return {"removed": False, "reason": "insufficient_interior"}
    old = sum(is_orange_sticker(before(x, y)) for x in xs)
    remaining = [x for x in xs if is_orange_sticker(after(x, y))]
    # Real deletion evidence can contain a single antialias/cursor-colored
    # sample. Accept only that isolated sample with >=95% empty interior;
    # never accept a two-column residual or a mostly empty pre-delete image.
    clean = not remaining or (len(remaining) == 1 and len(xs) >= 20)
    return {"removed": old >= len(xs) * 0.6 and clean,
            "interior_samples": len(xs), "before_orange": old,
            "remaining_orange": len(remaining), "remaining_x": remaining[:20],
            "isolated_pixel_ignored": clean and bool(remaining)}


def caption_view_matches(before: PixelAt, after: PixelAt, x0: int, x1: int, y: int, height: int) -> bool:
    """Compare caption occupancy, not sticker color/hover/selection styling."""
    if x1 <= x0:
        return False
    def red_column(reader, x):
        return sum(is_caption_red(reader(x, row)) for row in range(max(0, y-5), min(height, y+6))) >= 2
    changes = sum(red_column(before, x) != red_column(after, x) for x in range(x0, x1))
    return changes <= max(6, int((x1-x0)*.01))


def caption_horizontal_shift(before: PixelAt, after: PixelAt, x0: int, x1: int,
                             y: int, height: int) -> tuple[int,float] | None:
    """Find a unique horizontal translation of the caption blocks in one screen.

    Returns (after_x - before_x, Dice overlap). It never infers zoom or a
    different project from a similar-looking stripe alone.
    """
    if x1-x0 < 120:
        return None
    rows = range(max(0,y-5),min(height,y+6))
    old = [sum(is_caption_red(before(x,row)) for row in rows)>=2 for x in range(x0,x1)]
    new = [sum(is_caption_red(after(x,row)) for row in rows)>=2 for x in range(x0,x1)]
    def edges(bits):
        return [i for i in range(1,len(bits)) if bits[i] != bits[i-1]]
    old_edges,new_edges=edges(old),edges(new)
    if len(old_edges)<8 or len(new_edges)<8:
        return None
    counts: dict[int,int] = {}
    for a in old_edges:
        for b in new_edges:
            d=b-a
            counts[d]=counts.get(d,0)+1
    candidates=sorted(counts,key=counts.get,reverse=True)[:24]
    scores=[]
    size=len(old)
    for shift in candidates:
        left=max(0,-shift);right=min(size,size-shift)
        if right-left<max(200,size//4):
            continue
        old_part=old[left:right];new_part=new[left+shift:right+shift]
        positives_a=sum(old_part);positives_b=sum(new_part)
        if min(positives_a,positives_b)<30:
            continue
        common=sum(a and b for a,b in zip(old_part,new_part))
        scores.append((2*common/(positives_a+positives_b),shift))
    if not scores:
        return None
    scores.sort(reverse=True)
    best,shift=scores[0]
    runner_up=max((score for score,d in scores[1:] if abs(d-shift)>2),default=0)
    return (shift,best) if best>=.92 and best-runner_up>=.035 else None


def sticker_selection_frame(pixel_at: PixelAt, x: int, y: int, width: int, height: int) -> bool:
    """Require a near-white top and bottom selection edge around the click.

    A white material icon or vertical playhead alone cannot satisfy both edges.
    This validates the clicked sticker's appearance, not the application's
    internal linked-selection semantics.
    """
    if x < 4 or x >= width-4:
        return False
    # A cut handle / playhead can obscure the original seven-pixel probe.
    # Require a paired horizontal frame at one nearby column, with orange
    # material continuously connecting that column to the clicked position.
    # Do not borrow a frame across an empty gap from an adjacent segment.
    for center in (x, x+7, x-7, x+12, x-12):
        if center < 4 or center >= width-4:
            continue
        if center != x and not all(is_orange_sticker(pixel_at(col,y))
                                   for col in range(min(x,center), max(x,center)+1)):
            continue
        rows = []
        for row in range(max(0, y-36), min(height, y+37)):
            whites = sum(min(pixel_at(col,row)) >= 175 and max(pixel_at(col,row))-min(pixel_at(col,row)) <= 45
                         for col in range(center-3,center+4))
            if whites >= 5:
                rows.append(row)
        if any(top < y < bottom and 8 <= bottom-top <= 64 for top in rows for bottom in rows):
            return True
    return False


def selection_spread_outside_sticker(before: PixelAt, after: PixelAt, x0: int, x1: int,
                                     top: int, bottom: int, sticker_y: int, click_x: int) -> bool:
    """Detect newly highlighted outlines on other visible timeline tracks."""
    columns = set()
    count = 0
    def white(rgb):
        return min(rgb) >= 190 and max(rgb)-min(rgb) <= 35
    for y in range(top, bottom, 2):
        if abs(y-sticker_y) <= 32:
            continue
        for x in range(x0, x1, 2):
            if abs(x-click_x) <= 3:  # Ignore a moving thin playhead line.
                continue
            if white(after(x,y)) and not white(before(x,y)):
                count += 1
                columns.add(x)
                if count >= 24 and len(columns) >= 12:
                    return True
    return False
