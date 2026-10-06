import unittest
import tempfile
import json
from pathlib import Path
from local_slice_assistant.narration_plan import load_narration
from local_slice_assistant.models import Cut, Segment
from local_slice_assistant.narration_plan import import_narration, timeline_fingerprint, require_current_plan


class NarrationPlanTests(unittest.TestCase):
    def setUp(self):
        self.cut = Cut("成片", [Segment("a.mp4", 0, 2000000, "A"), Segment("b.mp4", 1000000, 4000000, "B")])
        self.raw = dict(schema_version=1, time_basis="output", timeline_fingerprint=timeline_fingerprint(self.cut),
                        cues=[dict(id="n1", text="解说", start_ms=1000, end_ms=3000)])

    def test_cross_segment_mapping(self):
        plan = import_narration(self.raw, self.cut)
        self.assertEqual([span["segment_id"] for span in plan["cues"][0]["spans"]], [s.id for s in self.cut.segments])
        self.assertEqual(plan["cues"][0]["original_audio"], "remove_dialogue")

    def test_reorder_invalidates_plan(self):
        plan = import_narration(self.raw, self.cut)
        self.cut.segments.reverse()
        with self.assertRaises(ValueError):
            require_current_plan(plan, self.cut)

    def test_wrong_time_basis(self):
        self.raw["time_basis"] = "source"
        with self.assertRaises(ValueError):
            import_narration(self.raw, self.cut)

    def test_overlap(self):
        self.raw["cues"].append(dict(id="n2", text="后句", start_ms=2000, end_ms=4000))
        with self.assertRaises(ValueError):
            import_narration(self.raw, self.cut)

    def test_muted_segment_cannot_supply_background_for_narration(self):
        self.cut.segments[0].original_audio = "mute"
        self.raw["timeline_fingerprint"] = timeline_fingerprint(self.cut)
        for mode in ("keep", "remove_dialogue"):
            self.raw["cues"][0]["original_audio"] = mode
            with self.assertRaisesRegex(ValueError, "片段已静音"):
                import_narration(self.raw, self.cut)
        self.raw["cues"][0]["original_audio"] = "mute"
        self.assertEqual(import_narration(self.raw, self.cut)["cues"][0]["original_audio"], "mute")

    def test_no_fingerprint_requires_confirmation(self):
        del self.raw["timeline_fingerprint"]
        with self.assertRaises(ValueError):
            import_narration(self.raw, self.cut)
        self.assertEqual(len(import_narration(self.raw, self.cut, allow_bind_current=True)["cues"]), 1)

    def test_standalone_json_srt_markdown_and_txt(self):
        with tempfile.TemporaryDirectory() as directory:
            for suffix, content in (
                ('.json', json.dumps(self.raw)),
                ('.srt', '1\n00:00:01,000 --> 00:00:03,000\n解说\n'),
                ('.md', '- **00:00:01.000–00:00:03.000** 解说'),
                ('.txt', '00:00:01.000 --> 00:00:03.000 解说'),
            ):
                path = Path(directory) / ('解说' + suffix)
                path.write_text(content, encoding='utf-8')
                plan = load_narration(path, self.cut, confirm_output_time=True)
                cue = plan['cues'][0]
                self.assertEqual((cue['start_us'], cue['end_us'], cue['text']), (1000000, 3000000, '解说'))
                self.assertEqual(len(cue['spans']), 2)
