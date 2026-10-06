"""Real local media checks for a simulated web/API planning response.

No model call, TTS, source separation, real drama or download is involved. The
AI planning quality and synthesized narration audio are deliberately not claimed
by this test; it checks edit execution and the two independent time bases.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from local_slice_assistant.exporter import export_cut
from local_slice_assistant.manifest import create_project_from_video, create_project_from_videos, import_planned_manifest
from local_slice_assistant.narration_plan import require_current_plan, timeline_fingerprint
from local_slice_assistant.planning_package import build_web_planning_package, write_web_planning_package
from local_slice_assistant.project_store import load_project, save_project
from local_slice_assistant.transcripts import load_transcript_files, map_cues_to_timeline
from tests.test_stage1_media_integration import _audio_at, _make_colored_media, _pixel_at


def _hashes(paths):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


class PlanningNarrationEndToEndTests(unittest.TestCase):
    def _run_case(self, *, multiple_sources: bool):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "合成剪辑与解说"
            root.mkdir()
            first = root / ("第01集.mp4" if multiple_sources else "已经粗剪过的视频.mp4")
            _make_colored_media(first, duration=10, color="red", fps=24, audio=True,
                                frequency=440, change_to=None if multiple_sources else "blue")
            videos = [first]
            if multiple_sources:
                second = root / "第02集.mp4"
                _make_colored_media(second, duration=8, color="blue", fps=30,
                                    audio=True, frequency=660)
                videos.append(second)
                document = create_project_from_videos(videos)
            else:
                document = create_project_from_video(first)
            source_hashes = _hashes(videos)

            # Independent transcriber outputs: an SRT for A and TXT for B.
            subtitle = root / "01.srt"
            subtitle.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n前因\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\n发展\n\n"
                "3\n00:00:06,000 --> 00:00:07,000\n后来的钩子\n\n"
                "4\n00:00:07,000 --> 00:00:08,000\n后来的悬念\n", encoding="utf-8")
            transcripts = [subtitle]
            bindings = {str(subtitle): first.name}
            if multiple_sources:
                other = root / "02.txt"
                other.write_text("00:00:02.000 --> 00:00:03.000 补充信息\n"
                                 "00:00:05.000 --> 00:00:06.000 第二集钩子\n"
                                 "00:00:06.000 --> 00:00:07.000 第二集悬念\n", encoding="utf-8")
                transcripts.append(other)
                bindings[str(other)] = second.name
            document.set_transcript_files([str(path) for path in transcripts], bindings=bindings)
            original_cues = load_transcript_files(document.transcript_paths, {}, source_bindings=bindings)
            package = build_web_planning_package(document, original_cues, include_narration=True,
                                                 objective="用后面钩子开头，再补前因，成片配短解说。")
            self.assertEqual(package["task_package_schema_version"], 4)
            self.assertIs(package["planning_request"]["include_narration"], True)
            self.assertEqual(package["response_contract"]["narration_contract"]["time_basis"], "output")
            package_path = write_web_planning_package(package, root / "上传给网页AI.json")
            self.assertEqual(json.loads(package_path.read_text(encoding="utf-8")), package)
            document.planning_context = {"package_id": package["package_id"], "package_path": str(package_path)}

            # These are SOURCE times. The cut order intentionally does not follow
            # source order; a retained range occurs twice as distinct segments.
            ranges = ([(second.name, 5000, 7000), (first.name, 1000, 4000),
                       (second.name, 2000, 3000), (second.name, 5000, 7000)]
                      if multiple_sources else
                      [(first.name, 6000, 8000), (first.name, 1000, 4000), (first.name, 6000, 8000)])
            duration_us = 8_000_000 if multiple_sources else 7_000_000
            second_cue_start = 6000 if multiple_sources else 5500
            second_cue_end = 7500 if multiple_sources else 6500
            response = dict(
                schema_version=1, example_only=False, drama=document.drama,
                planning_package_id=package["package_id"],
                sources=[{key: row[key] for key in ("file", "episode", "expected_duration_ms")}
                         for row in package["source_catalog"]],
                cuts=[dict(title="先钩子，再交代前因", segments=[
                    dict(file=name, in_ms=start, out_ms=end, original_audio="keep", purpose="合成验收")
                    for name, start, end in ranges],
                    narration=dict(schema_version=1, time_basis="output", cues=[
                        dict(id="n1", audio_filename="01_01_结果前因.wav", text="先看后面的结果，再看看它的起因。", start_ms=1500, end_ms=2500,
                             original_audio="remove_dialogue", background_gain_db=-12),
                        dict(id="n2", text="这一次回到悬念。", start_ms=second_cue_start, end_ms=second_cue_end,
                             original_audio="keep", background_gain_db=-18)]))])
            response_path = root / ("网页方案.txt" if multiple_sources else "网页方案.md")
            response_path.write_text("# 已确认设计\n先保留反应，不逐句删空隙。\n```json\n"
                                     + json.dumps(response, ensure_ascii=False, indent=2)
                                     + "\n```\n以上按原视频时间取段，解说按成片时间安排。\n", encoding="utf-8-sig")
            imported = import_planned_manifest(response_path, document)
            cut = imported.active_cut
            self.assertEqual([(segment.source_file, segment.in_us, segment.out_us) for segment in cut.segments],
                             [(name, start * 1000, end * 1000) for name, start, end in ranges])
            self.assertNotEqual(cut.segments[0].id, cut.segments[-1].id)
            self.assertEqual(imported.total_duration_us(), duration_us)
            plan = cut.packaging["narration_plan"]
            fingerprint = timeline_fingerprint(cut)
            self.assertEqual(plan["timeline_fingerprint"], fingerprint)
            self.assertEqual(plan["time_basis"], "output")
            self.assertEqual([(cue["start_us"], cue["end_us"]) for cue in plan["cues"]],
                             [(1_500_000, 2_500_000), (second_cue_start * 1000, second_cue_end * 1000)])
            first_cue = plan["cues"][0]
            self.assertEqual(first_cue['audio_filename'], '01_01_结果前因.wav')
            # The first narration crosses B->A (or late->early within the same
            # source), never accidentally using the 6s/1s SOURCE time as output.
            self.assertEqual([(span["segment_id"], span["output_in_us"], span["output_out_us"])
                              for span in first_cue["spans"]],
                             [(cut.segments[0].id, 1_500_000, 2_000_000),
                              (cut.segments[1].id, 2_000_000, 2_500_000)])
            self.assertEqual(first_cue["original_audio"], "remove_dialogue")
            self.assertEqual(first_cue["background_gain_db"], -12)
            require_current_plan(plan, cut)

            mapped = map_cues_to_timeline(cut, original_cues)
            # A's two retained lines have their one-second pause untouched.
            earlier = [cue for cue in mapped if cue.text in ("前因", "发展")]
            self.assertEqual([(cue.start_us, cue.end_us) for cue in earlier],
                             [(2_000_000, 3_000_000), (4_000_000, 5_000_000)])

            reopened = load_project(save_project(imported, root / "待配音粗剪.localcut.json")).document
            self.assertEqual(reopened.active_cut.packaging["narration_plan"], plan)
            self.assertEqual(timeline_fingerprint(reopened.active_cut), fingerprint)
            self.assertEqual(reopened.planning_context, imported.planning_context)
            require_current_plan(plan, reopened.active_cut)

            # A plan that requests narration but has no synthesized audio must
            # still permit a regular rough-cut export (include_packaging=False).
            output = export_cut(reopened)
            self.assertTrue(output.output_path.is_file())
            self.assertLessEqual(abs(output.observed_duration_us - duration_us), output.frame_duration_us)
            for seconds, channel in ((0.5, 2), (3.5, 0), (6.5, 2)):
                self.assertGreater(_pixel_at(output.output_path, seconds)[channel], 170)
            self.assertAlmostEqual(_audio_at(output.output_path, 3.5)[0], 440, delta=35)
            if multiple_sources:
                self.assertAlmostEqual(_audio_at(output.output_path, 0.5)[0], 660, delta=35)
                self.assertAlmostEqual(_audio_at(output.output_path, 6.5)[0], 660, delta=35)
            self.assertEqual(_hashes(videos), source_hashes)

            # Subsequent edit invalidates narration. Undo restores the exact
            # fingerprint and plan rather than rebinding an out-of-date script.
            reopened.adjust_segment_end(reopened.active_cut.id, 0, 100_000)
            with self.assertRaisesRegex(ValueError, "重新对齐"):
                require_current_plan(plan, reopened.active_cut)
            self.assertTrue(reopened.undo())
            self.assertEqual(timeline_fingerprint(reopened.active_cut), fingerprint)
            require_current_plan(plan, reopened.active_cut)
            self.assertEqual(_hashes(videos), source_hashes)

    def test_single_rough_cut_md_plan_with_output_time_narration_and_real_export(self):
        self._run_case(multiple_sources=False)

    def test_two_episode_txt_plan_with_reorder_repeat_narration_and_real_export(self):
        self._run_case(multiple_sources=True)


if __name__ == "__main__":
    unittest.main()
