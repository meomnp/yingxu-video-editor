from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_slice_assistant.errors import ManifestValidationError
from local_slice_assistant.manifest import import_manifest, import_planned_manifest
from local_slice_assistant.plan_response import (
    MAX_PLAN_RESPONSE_BYTES, decode_plan_response, read_plan_response,
)
from tests.helpers import make_empty_sources, patched_probe, standard_manifest, write_manifest


class PlanResponseDecodingTests(unittest.TestCase):
    def test_plain_json_and_bom_preserve_all_fields(self):
        expected = standard_manifest()
        expected["planning_package_id"] = "本次任务"
        self.assertEqual(decode_plan_response("\ufeff" + json.dumps(expected)), expected)

    def test_markdown_and_txt_allow_design_prose_around_one_json_block(self):
        expected = standard_manifest()
        for fence in ("```json", "```JSON", "~~~~json", "  ``` json"):
            end = "~~~~" if fence == "~~~~json" else "```"
            with self.subTest(fence=fence):
                text = f"# 设计\n先用第19集做钩子。\n\n{fence}\n{json.dumps(expected)}\n{end}\n\n后续核对人物反应。"
                self.assertEqual(decode_plan_response(text), expected)

    def test_unlabelled_fence_or_srt_or_prose_is_not_guessed(self):
        for text in ("```\n{}\n```", "先剪第1集再剪第2集", "1\n00:00:01,000 --> 00:00:02,000\n你好",
                     "| 文件 | 入点 | 出点 |\n|01.mp4|1|2|", "说明\n{\"cuts\": []}"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ManifestValidationError, "没有找到可执行"):
                    decode_plan_response(text)

    def test_multiple_candidate_blocks_are_rejected_even_when_one_is_invalid(self):
        for first in ("{}", "invalid"):
            with self.assertRaisesRegex(ManifestValidationError, "多个 JSON"):
                decode_plan_response(f"```json\n{first}\n```\n设计二\n```json\n{{}}\n```")
        with self.assertRaisesRegex(ManifestValidationError, "多个 JSON"):
            decode_plan_response("```json\n{}\n```\n另一个方案\n```\n{}\n```")

    def test_unclosed_json_block_is_rejected(self):
        with self.assertRaisesRegex(ManifestValidationError, "没有结束"):
            decode_plan_response("```json\n{}")

    def test_plain_concatenated_json_or_trailing_prose_is_rejected(self):
        for text in ("{} {}", "{}\n上面就是方案", "{}\n```json\n{}\n```"):
            with self.assertRaisesRegex(ManifestValidationError, "不是有效 JSON"):
                decode_plan_response(text)

    def test_duplicate_keys_are_rejected_at_any_depth(self):
        for text in ('{"schema_version":1,"schema_version":2}',
                     '{"sources":[{"file":"01.mp4","file":"02.mp4"}]}',
                     '{"x": 1, "\\u0078": 2}'):
            for wrapped in (text, "```json\n" + text + "\n```"):
                with self.assertRaisesRegex(ManifestValidationError, "重复 JSON"):
                    decode_plan_response(wrapped)

    def test_nonfinite_constants_and_overflowing_float_are_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(value=value), self.assertRaisesRegex(ManifestValidationError, "非有限"):
                decode_plan_response('{"value":' + value + '}')

    def test_array_root_and_empty_text_are_rejected(self):
        with self.assertRaisesRegex(ManifestValidationError, "根节点"):
            decode_plan_response("[]")
        with self.assertRaisesRegex(ManifestValidationError, "为空"):
            decode_plan_response("\ufeff \n")

    def test_deep_nesting_has_user_facing_error(self):
        with self.assertRaisesRegex(ManifestValidationError, "嵌套过深"):
            decode_plan_response('{"nested":' + "[" * 1500 + "0" + "]" * 1500 + "}")

    def test_escaped_invalid_unicode_is_rejected_before_utf8_save(self):
        for text in ('{"title":"\\ud800"}', '{"\\udfff":"value"}'):
            with self.assertRaisesRegex(ManifestValidationError, "Unicode"):
                decode_plan_response(text)

    def test_file_bom_and_invalid_encoding_and_missing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "方案.md"
            path.write_text('正文\n```json\n{"schema_version": 1}\n```', encoding="utf-8-sig")
            self.assertEqual(read_plan_response(path), {"schema_version": 1})
            path.write_bytes(b"\xff\xfeinvalid")
            with self.assertRaisesRegex(ManifestValidationError, "UTF-8"):
                read_plan_response(path)
            with self.assertRaisesRegex(ManifestValidationError, "找不到"):
                read_plan_response(Path(folder) / "missing.json")
            with self.assertRaisesRegex(ManifestValidationError, "无法读取"):
                read_plan_response(folder)

    def test_file_read_is_bounded_and_text_bytes_are_bounded(self):
        handle = io.BytesIO(b" " * (MAX_PLAN_RESPONSE_BYTES + 1))
        with patch.object(Path, "open", return_value=handle):
            with self.assertRaisesRegex(ManifestValidationError, "16 MiB"):
                read_plan_response("large.txt")
        with patch("local_slice_assistant.plan_response.MAX_PLAN_RESPONSE_BYTES", 10):
            with self.assertRaisesRegex(ManifestValidationError, "16 MiB"):
                decode_plan_response("一" * 4)


class PlanResponseManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        make_empty_sources(self.root)
        with patched_probe():
            self.before = import_manifest(write_manifest(self.root), self.root)
        self.before.planning_context = {"package_id": "current-package", "objective": "跨集重排"}
        self.before.set_transcript_files(["19.srt"], bindings={"19.srt": "19.mp4"})

    def _response(self):
        raw = standard_manifest()
        raw["planning_package_id"] = "current-package"
        return raw

    def _write(self, raw, suffix=".md"):
        path = self.root / ("AI方案" + suffix)
        path.write_text("# 剪辑设计\n第19集做钩子，再补前因。\n```json\n" + json.dumps(raw, ensure_ascii=False)
                        + "\n```\n请预览确认节奏。", encoding="utf-8-sig")
        return path

    def test_md_txt_and_json_file_import_preserve_order_context_and_optional_fields(self):
        raw = self._response()
        segment = raw["cuts"][0]["segments"][0]
        segment.update(first_line="开始", last_line="结束")
        raw["cuts"][0]["publishing"] = dict(kind="video", title="标题", body="正文",
                                          tags=["#合成验收剧", "#短剧", "#剧情", "#反转", "#冲突", "#切片"])
        raw["cuts"][0]["narration"] = dict(schema_version=1, time_basis="output", cues=[
            dict(id="n1", text="人物陷入困境。", start_ms=1000, end_ms=2000,
                 original_audio="remove_dialogue", background_gain_db=-12)])
        old = copy.deepcopy(self.before.to_dict())
        for suffix in (".md", ".txt", ".json"):
            with self.subTest(suffix=suffix), patched_probe():
                path = self._write(raw, suffix)
                imported = import_planned_manifest(path, self.before)
                self.assertEqual([s.source_file for s in imported.active_cut.segments],
                                 ["19.mp4", "16.mp4", "17.mp4", "18.mp4", "19.mp4"])
                first = imported.active_cut.segments[0]
                self.assertEqual((first.first_line, first.last_line, first.purpose), ("开始", "结束", "钩子"))
                self.assertEqual(imported.total_duration_us(), 195_000_000)
                self.assertEqual(imported.original_manifest, raw)
                self.assertEqual(imported.transcript_bindings, self.before.transcript_bindings)
                self.assertEqual(imported.planning_context["package_id"], "current-package")
                self.assertEqual(imported.active_cut.publishing["body"], "正文")
                self.assertEqual(imported.active_cut.packaging["narration_plan"]["cues"][0]["start_us"], 1_000_000)
        self.assertEqual(self.before.to_dict(), old)

    def test_package_binding_cannot_be_bypassed_by_markdown_wrapper(self):
        raw = self._response()
        raw["planning_package_id"] = "old-package"
        with patch("local_slice_assistant.manifest.probe_media") as probe:
            with self.assertRaisesRegex(ManifestValidationError, "不对应当前任务包"):
                import_planned_manifest(self._write(raw), self.before)
            probe.assert_not_called()

    def test_unknown_fields_at_every_instruction_level_fail_before_probe(self):
        for level, field in (("top", "transition"), ("source", "url"), ("cut", "speed"),
                             ("segment", "speed"), ("segment", "speed_percent"), ("segment", "transition")):
            with self.subTest(level=level, field=field):
                raw = self._response()
                target = {"top": raw, "source": raw["sources"][-1], "cut": raw["cuts"][-1],
                          "segment": raw["cuts"][-1]["segments"][-1]}[level]
                target[field] = 1
                with patch("local_slice_assistant.manifest.probe_media") as probe:
                    with self.assertRaisesRegex(ManifestValidationError, "未支持字段.*" + field):
                        import_planned_manifest(self._write(raw), self.before)
                    probe.assert_not_called()

    def test_boolean_and_float_schema_are_not_version_one(self):
        for version in (True, 1.0, "1", None):
            raw = self._response()
            raw["schema_version"] = version
            with self.subTest(version=version), patch("local_slice_assistant.manifest.probe_media") as probe:
                with self.assertRaisesRegex(ManifestValidationError, "schema_version"):
                    import_planned_manifest(self._write(raw), self.before)
                probe.assert_not_called()

    def test_late_invalid_range_and_audio_type_are_validated_before_probe(self):
        for field, value, message in (("in_ms", -1, "不能小于"), ("out_ms", 0, "必须大于"),
                                      ("original_audio", [], "original_audio")):
            raw = self._response()
            raw["cuts"][-1]["segments"][-1][field] = value
            with self.subTest(field=field), patch("local_slice_assistant.manifest.probe_media") as probe:
                with self.assertRaisesRegex(ManifestValidationError, message):
                    import_planned_manifest(self._write(raw), self.before)
                probe.assert_not_called()

    def test_unknown_publishing_and_narration_fields_are_not_silently_ignored(self):
        for layer in ("publishing", "narration", "cue"):
            raw = self._response()
            cut = raw["cuts"][0]
            if layer == "publishing":
                cut["publishing"] = {"surprise": "ignored before"}
            else:
                cut["narration"] = dict(schema_version=1, time_basis="output", cues=[
                    dict(id="n1", text="配音", start_ms=0, end_ms=1000)])
                target = cut["narration"] if layer == "narration" else cut["narration"]["cues"][0]
                target["surprise"] = "ignored before"
            with self.subTest(layer=layer), patch("local_slice_assistant.manifest.probe_media") as probe:
                with self.assertRaisesRegex(ManifestValidationError, "未支持字段.*surprise"):
                    import_planned_manifest(self._write(raw), self.before)
                probe.assert_not_called()

    def test_import_checks_and_executes_one_read_not_changed_second_read(self):
        raw = self._response()
        with patched_probe(), patch("local_slice_assistant.manifest.read_plan_response", return_value=raw) as reader:
            imported = import_planned_manifest("response.md", self.before)
        self.assertEqual(imported.original_manifest, raw)
        reader.assert_called_once()


if __name__ == "__main__":
    unittest.main()
