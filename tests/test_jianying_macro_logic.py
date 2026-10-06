from __future__ import annotations

import unittest

from local_slice_assistant.jianying_macro_logic import (
    DpiMapping,
    Gap,
    MacroValidationError,
    Point,
    caption_runs,
    gap_has_continuous_sticker,
    gaps_between,
    is_jianying_executable,
    safe_sticker_gaps,
    remaining_sticker_gaps,
    deletion_evidence,
    caption_view_matches,
    snapshot_still_matches,
    validate_detection_points,
)


def _pixels(width: int, height: int, values: dict[tuple[int, int], tuple[int, int, int]]):
    return lambda x, y: values.get((x, y), (20, 20, 20))


class JianyingMacroLogicTests(unittest.TestCase):
    def test_view_check_ignores_sticker_highlight_but_rejects_caption_scroll(self):
        def original(x,y):
            return (210,70,50) if 20<=x<50 and 15<=y<26 else (20,20,20)
        def hover(x,y):
            return (255,255,255) if y==40 else original(x,y)
        def scroll(x,y):
            return original(x-15,y)
        self.assertTrue(caption_view_matches(original,hover,0,100,20,80))
        self.assertFalse(caption_view_matches(original,scroll,0,100,20,80))

    def test_deletion_ignores_edge_sliver_but_rejects_interior_sticker(self):
        gap = Gap(10, 40)
        before = lambda x,y:(210,120,40)
        edge_only = lambda x,y:(210,120,40) if x < 13 or x >= 37 else (30,30,30)
        self.assertTrue(deletion_evidence(before, edge_only, gap, 30)["removed"])
        self.assertFalse(deletion_evidence(before, before, gap, 30)["removed"])
        self.assertTrue(deletion_evidence(before, lambda x,y:(210,120,40) if x==25 else (30,30,30), gap, 30)["removed"])
        self.assertFalse(deletion_evidence(before, lambda x,y:(210,120,40) if x in (25,26) else (30,30,30), gap, 30)["removed"])
        self.assertFalse(deletion_evidence(before, lambda x,y:(210,120,40) if x==15 else (30,30,30), Gap(10,20), 30)["removed"])
        self.assertFalse(deletion_evidence(edge_only, edge_only, gap, 30)["removed"])

    def test_existing_cut_edges_do_not_hide_remaining_sticker(self):
        gap = Gap(10, 40)
        def cut_strip(x, y):
            return (30, 30, 30) if x in (10, 11, 25, 39) else (210, 120, 40)
        self.assertEqual(remaining_sticker_gaps(cut_strip, [gap], 30), [gap])
        self.assertEqual(remaining_sticker_gaps(lambda x,y:(30,30,30), [gap], 30), [])
        self.assertTrue(snapshot_still_matches(cut_strip, cut_strip, gap, 30))

    def test_fine_detection_preserves_narrow_caption_and_ignores_text_holes(self):
        def pixels(x, y):
            if (10 <= x < 30 or 40 <= x < 42) and 45 <= y <= 55:
                if 16 <= x < 24 and 48 <= y <= 52:
                    return (255, 255, 255)
                return (200, 80, 60)
            return (20, 20, 20)
        runs = caption_runs(pixels, 80, 100, 0, 70, 50, min_width=1, row_radius=5, min_votes=2)
        self.assertEqual(runs, [(10, 30), (40, 42)])
        self.assertEqual(gaps_between(runs, margin=0, min_gap_width=10), [Gap(30, 40)])

    def test_caption_blocks_and_internal_gaps_ignore_short_noise(self) -> None:
        values: dict[tuple[int, int], tuple[int, int, int]] = {}
        for start, end in ((10, 18), (30, 32), (42, 51)):
            for x in range(start, end):
                for y in range(48, 53):
                    values[x, y] = (210, 30, 25)
        runs = caption_runs(_pixels(80, 100, values), 80, 100, 0, 70, 50)
        self.assertEqual(runs, [(10, 18), (42, 51)])
        self.assertEqual(gaps_between(runs), [Gap(20, 40)])

    def test_gap_rules_reject_end_gaps_and_keep_margin(self) -> None:
        self.assertEqual(
            gaps_between([(10, 20), (40, 50), (70, 80)]),
            [Gap(22, 38), Gap(52, 68)],
        )
        self.assertEqual(gaps_between([(10, 20)]), [])
        with self.assertRaises(MacroValidationError):
            gaps_between([(20, 30), (10, 15)])

    def test_detection_points_require_distinct_tracks_and_wide_range(self) -> None:
        points = [Point(5, 10), Point(5, 28), Point(5, 50), Point(10, 70), Point(60, 70)]
        checked = validate_detection_points(points, 100, 100)
        self.assertEqual((checked.left, checked.right), (10, 60))
        with self.assertRaises(MacroValidationError):
            validate_detection_points([Point(5, 10), Point(5, 12), *points[2:]], 100, 100)
        with self.assertRaises(MacroValidationError):
            validate_detection_points([*points[:4], Point(30, 70)], 100, 100)

    def test_dpi_mapping_maps_edges_and_rejects_aspect_change(self) -> None:
        mapping = DpiMapping.create(1280, 720, 1920, 1080, 1920, 1080)
        self.assertEqual(mapping.logical_to_image(Point(0, 0)), Point(0, 0))
        self.assertEqual(mapping.logical_to_image(Point(1279, 719)), Point(1919, 1079))
        self.assertEqual(mapping.image_to_screen(Point(1919, 1079)), Point(1919, 1079))
        with self.assertRaises(MacroValidationError):
            DpiMapping.create(1280, 720, 1600, 900, 1920, 1200)

    def test_sticker_and_snapshot_checks_fail_closed(self) -> None:
        orange = (210, 120, 40)
        baseline_values = {(x, 30): orange for x in range(10, 20)}
        baseline = _pixels(50, 50, baseline_values)
        gap = Gap(10, 20)
        self.assertTrue(gap_has_continuous_sticker(baseline, gap, 30))
        self.assertEqual(safe_sticker_gaps(baseline, [gap], 30), [gap])
        unchanged = _pixels(50, 50, {(x, 30): (215, 118, 43) for x in range(10, 20)})
        self.assertTrue(snapshot_still_matches(baseline, unchanged, gap, 30))
        changed_values = dict(baseline_values)
        changed_values[15, 30] = (30, 30, 30)
        changed = _pixels(50, 50, changed_values)
        self.assertFalse(gap_has_continuous_sticker(changed, gap, 30))
        self.assertFalse(snapshot_still_matches(baseline, changed, gap, 30))

    def test_only_exact_jianying_process_name_is_accepted(self) -> None:
        self.assertTrue(is_jianying_executable('"C:\\Program Files\\JianyingPro\\JianyingPro.exe"'))
        self.assertTrue(is_jianying_executable("C:/Apps/JianyingPro.EXE"))
        self.assertFalse(is_jianying_executable("C:\\Apps\\JianyingPro.exe.bak"))
        self.assertFalse(is_jianying_executable("C:\\Apps\\other.exe"))


if __name__ == "__main__":
    unittest.main()
