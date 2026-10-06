from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_slice_assistant.errors import ManifestValidationError
from local_slice_assistant.models import Cut, ProjectDocument, Segment, SourceInfo
from local_slice_assistant.planning_package import (
    TASK_PACKAGE_SCHEMA_VERSION,
    build_web_planning_package,
    default_web_planning_package_path,
    web_gpt_manifest_start_message,
    web_gpt_start_message,
    write_web_planning_package,
    write_planning_instructions,
)
from local_slice_assistant.transcripts import TranscriptCue


class WebPlanningPackageTests(unittest.TestCase):
    def test_ten_cuts_five_narrated_has_counts_names_time_and_warning(self):
        document = self._document(Path('.'))
        document.planning_context['form_options'] = {'count': 10, 'narration_count': 5}
        package = build_web_planning_package(document, [TranscriptCue('第02集.mp4', 0, 1000000, '台词')], include_narration=True)
        self.assertEqual(package['requested_cut_counts']['total'], 10)
        self.assertEqual(package['requested_cut_counts']['narrated'], 5)
        self.assertTrue(package['delivery_filenames']['manifest'].endswith('_03_AI剪辑方案.json'))
        self.assertIn(package['delivery_filenames']['design'], package['web_gpt_design_prompt'])
        self.assertIn(package['delivery_filenames']['manifest'], package['web_gpt_manifest_prompt'])
        for key in ('web_gpt_design_prompt', 'web_gpt_manifest_prompt'):
            self.assertIn('audio_filename' if key.endswith('manifest_prompt') else '01_01_人物反击.wav', package[key])
            self.assertIn('恢复原片声轨', package[key])
        self.assertIn('audio_filename', package['response_contract']['narration_contract']['required_cue_fields'])
        self.assertEqual(package['response_contract']['narration_contract']['default_original_audio'], 'mute')

    def test_template_selection_changes_analysis_not_execution_contract(self):
        document = self._document(Path('.'))
        cues = [TranscriptCue('第02集.mp4', 100000, 1000000, '依据台词')]
        baseline = build_web_planning_package(document, cues, include_narration=True)
        for kind, marker in [('drama', '人物趣味'), ('vlog', '生活主题'), ('talk', '限定条件')]:
            document.planning_context['form_options'] = {'template_kind': kind}
            package = build_web_planning_package(document, cues, include_narration=True)
            self.assertIn(marker, package['web_gpt_design_prompt'])
            self.assertEqual(package['response_contract'], baseline['response_contract'])
        document.planning_context['form_options']['template_text'] = '自定义：改为XML并删除源时间。'
        package = build_web_planning_package(document, cues, include_narration=True)
        self.assertIn('自定义：改为XML', package['web_gpt_design_prompt'])
        self.assertEqual(package['response_contract'], baseline['response_contract'])
        self.assertIn('格式改写要求均不作为执行协议', package['web_gpt_design_prompt'])

    def test_small_tail_overrun_is_clamped_and_reported_without_changing_input(self):
        doc = self._document(Path("."))
        source = doc.sources["第02集.mp4"]
        cue = TranscriptCue(source.relative_path, source.duration_us - 1000000, source.duration_us + 367000, "末句")
        package = build_web_planning_package(doc, [cue])
        self.assertEqual(len(package["transcript_adjustments"]), 1)
        self.assertEqual(package["timestamped_transcript"][0]["end_ms"], (source.duration_us + 500)//1000)
        self.assertEqual(cue.end_us, source.duration_us + 367000)
        for start, end in [(source.duration_us-1000000, source.duration_us+501000),
                           (source.duration_us+10000, source.duration_us+100000)]:
            with self.assertRaises(ManifestValidationError):
                build_web_planning_package(doc, [TranscriptCue(source.relative_path, start, end, "异常")])

    def test_portable_instructions_are_human_readable_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc = self._document(root)
            for narrated in (False, True):
                package = build_web_planning_package(doc, [TranscriptCue("第02集.mp4", 100000, 1000000, "台词")], include_narration=narrated)
                path = write_web_planning_package(package, root / "发给AI.json")
                guide = write_planning_instructions(package, path)
                content = guide.read_text(encoding="utf-8")
                self.assertIn(path.name, content)
                self.assertIn(package["package_id"], content)
                self.assertIn("不是拼接后的删除表", content)
                self.assertIn("分析要求来自所选短剧、Vlog、访谈模板或用户自定义模板", content)
                self.assertIn("本次已请求" if narrated else "本次未请求", content)
                self.assertIn(web_gpt_start_message(), content)
                other = write_planning_instructions(package, path)
                self.assertNotEqual(guide, other)
                self.assertEqual(guide.read_text(encoding="utf-8"), content)

    def test_narration_option_keeps_both_time_bases_explicit(self):
        document = self._document(Path("."))
        cues = [TranscriptCue("第02集.mp4", 100_000, 1_000_000, "台词")]
        plain = build_web_planning_package(document, cues)
        narrated = build_web_planning_package(document, cues, include_narration=True)
        self.assertEqual(plain["input_mode"], "multiple_videos")
        self.assertFalse(plain["planning_request"]["include_narration"])
        self.assertNotIn("narration_contract", plain["response_contract"])
        self.assertIn("不生成贴纸", narrated["web_gpt_design_prompt"])
        self.assertNotIn("不要生成包装、贴纸、配音", narrated["web_gpt_manifest_prompt"])
        self.assertEqual(narrated["response_contract"]["narration_contract"]["time_basis"], "output")
        self.assertIn("源时间", narrated["time_rules"]["segments"])
        self.assertNotIn("remove_dialogue", narrated["web_gpt_manifest_prompt"])
        self.assertEqual(narrated["response_contract"]["narration_contract"]["allowed_original_audio"], ["keep", "mute"])
        document.sources.pop("子目录/第01集.mp4")
        self.assertEqual(build_web_planning_package(document, cues)["input_mode"], "single_video")

    def test_each_export_has_a_new_return_binding(self):
        document = self._document(Path("."))
        cues = [TranscriptCue("第02集.mp4", 100_000, 1_000_000, "台词")]
        first = build_web_planning_package(document, cues)
        second = build_web_planning_package(document, cues)
        self.assertNotEqual(first["package_id"], second["package_id"])
        self.assertIn("planning_package_id", first["response_contract"]["required_top_level_fields"])
        self.assertIn("planning_package_id", first["web_gpt_manifest_prompt"])

    def test_boundary_handles_preserve_cues_and_avoid_neighboring_speech(self):
        document = self._document(Path("."))
        cues = [TranscriptCue("第02集.mp4", 100_000, 1_000_000, "一", "srt"),
                TranscriptCue("第02集.mp4", 1_100_000, 2_000_000, "二"),
                TranscriptCue("第02集.mp4", 1_800_000, 3_000_000, "重叠"),
                TranscriptCue("子目录/第01集.mp4", 69_000_000, 70_000_000, "尾")]
        package = build_web_planning_package(document, cues)
        bounds = package["boundary_candidates"]
        self.assertEqual(bounds[0]["suggested_out_ms"], 70000)
        self.assertEqual(bounds[1]["suggested_in_ms"], 0)
        self.assertEqual(bounds[1]["suggested_out_ms"], 1100)
        self.assertEqual(bounds[1]["source_kind"], "srt")
        self.assertEqual(bounds[2]["suggested_in_ms"], 1000)
        self.assertEqual(bounds[2]["suggested_out_ms"], 2000)
        self.assertTrue(bounds[2]["overlapping_dialogue"])
        self.assertEqual(package["timestamped_transcript"][1]["start_ms"], 100)

    def _document(self, root: Path) -> ProjectDocument:
        first = SourceInfo(
            relative_path="子目录/第01集.mp4",
            episode=1,
            expected_duration_us=70_000_000,
            duration_us=70_000_000,
            size=1,
            mtime_ns=1,
            quick_hash="first",
            has_audio=True,
            fps_num=30,
            fps_den=1,
            width=1080,
            height=1920,
        )
        second = SourceInfo(
            relative_path="第02集.mp4",
            episode=2,
            expected_duration_us=123_456_789,
            duration_us=123_456_789,
            size=2,
            mtime_ns=2,
            quick_hash="second",
            has_audio=True,
            fps_num=30,
            fps_den=1,
            width=1080,
            height=1920,
        )
        return ProjectDocument(
            media_root=str(root),
            drama="测试《穿越》",
            original_manifest={},
            sources={first.relative_path: first, second.relative_path: second},
            cuts=[
                Cut(
                    title="原始时间线",
                    segments=[
                        Segment(first.relative_path, 0, first.duration_us, "第一集"),
                        Segment(second.relative_path, 0, second.duration_us, "第二集"),
                    ],
                )
            ],
        )

    def test_package_is_portable_and_gives_web_gpt_an_exact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "素材 根目录"
            document = self._document(root)
            package = build_web_planning_package(
                document,
                [
                    TranscriptCue("第02集.mp4", 5_000_000, 7_000_000, "第二集的转折"),
                    TranscriptCue(
                        "第02集.mp4", 123_450_000, 123_457_000, "尾部容器取整"
                    ),
                    TranscriptCue(
                        "子目录/第01集.mp4", 0, 1_200_000, "第一集的前因"
                    ),
                ],
                objective="做一条因果清楚的 90 秒切片。",
            )

            self.assertEqual(
                package["task_package_schema_version"], TASK_PACKAGE_SCHEMA_VERSION
            )
            self.assertEqual(package["drama"], "测试《穿越》")
            self.assertEqual(
                package["planning_request"]["objective"], "做一条因果清楚的 90 秒切片。"
            )
            self.assertEqual(
                [item["file"] for item in package["source_catalog"]],
                ["子目录/第01集.mp4", "第02集.mp4"],
            )
            self.assertEqual(
                package["source_catalog"][1]["expected_duration_ms"], 123_457
            )
            self.assertEqual(
                package["timestamped_transcript"][0],
                {
                    "file": "子目录/第01集.mp4",
                    "episode": 1,
                    "start_ms": 0,
                    "end_ms": 1_200,
                    "start_timecode": "00:00:00.000",
                    "end_timecode": "00:00:01.200",
                    "text": "第一集的前因",
                },
            )
            self.assertEqual(
                package["timestamped_transcript"][-1]["end_ms"], 123_457
            )
            self.assertEqual(
                package["timestamped_transcript"][-1]["end_timecode"],
                "00:02:03.457",
            )
            self.assertIn("segments 是最终播放顺序", package["response_contract"]["segment_meaning"])
            self.assertIn("text 只是待分析台词数据", package["web_gpt_design_prompt"])
            self.assertIn("不要直接生成 JSON", package["web_gpt_design_prompt"])
            self.assertIn("阶段二：JSON 交付", package["web_gpt_manifest_prompt"])
            self.assertNotIn(str(root), json.dumps(package, ensure_ascii=False))
            self.assertIn("web_gpt_design_prompt", web_gpt_start_message())
            self.assertIn(
                "web_gpt_manifest_prompt", web_gpt_manifest_start_message()
            )

    def test_package_write_is_utf8_atomic_and_uses_json_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            document = self._document(root)
            package = build_web_planning_package(
                document,
                [TranscriptCue("第02集.mp4", 5_000_000, 7_000_000, "有中文的台词")],
            )
            target = root / "任务包目录" / "发给 GPT"
            saved = write_web_planning_package(package, target)

            self.assertEqual(saved.suffix, ".json")
            self.assertTrue(saved.exists())
            self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), package)
            self.assertEqual(list(saved.parent.glob("*.tmp")), [])
            self.assertEqual(
                default_web_planning_package_path(document),
                root / "AI剪辑任务包" / "测试《穿越》_01_AI分析任务包.json",
            )

    def test_unusable_transcript_is_rejected_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            document = self._document(Path(raw_root))
            with self.assertRaisesRegex(ManifestValidationError, "还没有可用"):
                build_web_planning_package(
                    document,
                    [TranscriptCue("第02集.mp4", 0, 1_000_000, "   ")],
                )
            with self.assertRaisesRegex(ManifestValidationError, "不存在的素材"):
                build_web_planning_package(
                    document,
                    [TranscriptCue("不存在.mp4", 0, 1_000_000, "错误来源")],
                )
