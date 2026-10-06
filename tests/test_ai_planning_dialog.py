import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox

from local_slice_assistant.ai_planning_dialog import ApiPlanningDialog, PlanningOptionsDialog
from local_slice_assistant.gui import MainWindow
from local_slice_assistant.manifest import import_manifest
from local_slice_assistant.plan_review import PlanReviewDialog
from local_slice_assistant.project_store import load_project, save_project
from local_slice_assistant.transcripts import TranscriptCue
from tests.helpers import make_empty_sources, patched_probe, standard_manifest, write_manifest


class ApiPlanningDialogTests(unittest.TestCase):
    def test_import_checks_exact_five_narrated_of_ten_and_keeps_names(self):
        from local_slice_assistant.manifest import import_planned_manifest
        from local_slice_assistant.errors import ManifestValidationError
        raw = standard_manifest()
        prototype = copy.deepcopy(raw['cuts'][0])
        raw['cuts'] = [dict(copy.deepcopy(prototype), title=f'方案{i+1}') for i in range(10)]
        for index in range(5):
            raw['cuts'][index]['narration'] = dict(schema_version=1, time_basis='output', cues=[dict(
                id=f'{index+1:02d}_01', audio_filename=f'{index+1:02d}_01_主题.wav', text='解说台词',
                start_ms=1000, end_ms=3500, original_audio='mute', background_gain_db=-12)])
        self.document.planning_context['form_options'] = dict(count=10, narration_count=5)
        target = self.root/'十条方案.json'
        target.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
        with patched_probe():
            imported = import_planned_manifest(target, self.document)
        self.assertEqual(sum(bool(cut.packaging.get('narration_plan')) for cut in imported.cuts), 5)
        self.assertEqual(imported.cuts[4].packaging['narration_plan']['cues'][0]['audio_filename'], '05_01_主题.wav')
        raw['cuts'][4].pop('narration')
        target.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
        with patched_probe(), self.assertRaisesRegex(ManifestValidationError, '解说条数'):
            import_planned_manifest(target, self.document)

    def test_large_batch_goal_roundtrip_and_diversity_instructions(self):
        from local_slice_assistant.models import ProjectDocument
        dialog = PlanningOptionsDialog(self.document)
        for count in (30, 100):
            dialog.count.setValue(count)
            dialog.narration_count.setValue(12)
            self.assertEqual(dialog.count.value(), count)
            self.document.planning_context['form_options'] = dialog.form_options()
            restored = ProjectDocument.from_dict(self.document.to_dict())
            self.assertEqual(restored.planning_context['form_options']['count'], count)
        self.assertIn('方案差异表', dialog.planning_objective())
        self.assertIn('无法判断跨批雷同', dialog.planning_objective())
        dialog.reject()

    def test_template_import_invalid_file_keeps_previous_and_reset_keeps_type(self):
        dialog = PlanningOptionsDialog(self.document)
        dialog.template_kind.setCurrentIndex(dialog.template_kind.findData("talk"))
        path = self.root / "分析模板.md"
        path.write_text("保留问题与回答的完整语境", encoding="utf-8-sig")
        with patch("local_slice_assistant.ai_planning_dialog.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            dialog.import_template()
            self.assertEqual(dialog.template_text, "保留问题与回答的完整语境")
            path.write_bytes(b"x" * 200001)
            with patch("local_slice_assistant.ai_planning_dialog.QMessageBox.warning") as warning:
                dialog.import_template()
                warning.assert_called_once()
            self.assertEqual(dialog.template_text, "保留问题与回答的完整语境")
            path.write_bytes(b"\xff\x00")
            with patch("local_slice_assistant.ai_planning_dialog.QMessageBox.warning") as warning:
                dialog.import_template()
                warning.assert_called_once()
            self.assertEqual(dialog.template_text, "保留问题与回答的完整语境")
        dialog.reset_template()
        self.assertEqual(dialog.template_text, "")
        self.assertEqual(dialog.template_kind.currentData(), "talk")
        dialog.close()

    def test_fill_in_goal_and_details_keep_duration_and_restore_draft(self):
        dialog = PlanningOptionsDialog(self.document)
        dialog.duration.setText("5分钟左右，允许误差30秒")
        dialog.count.setValue(3)
        dialog.narration_count.setValue(2)
        dialog.objective.setPlainText("保留笑点")
        dialog.template_kind.setCurrentIndex(dialog.template_kind.findData("vlog"))
        dialog.template_text = "按旅行日记分析，不强制制造冲突。"
        dialog.template_name = "旅行模板.md"
        brief = dialog.planning_objective()
        self.assertIn("3 条", brief)
        self.assertIn("5分钟左右", brief)
        self.assertIn("精简版", brief)
        self.assertIn("原声 1 条、带解说 2 条", brief)
        self.assertNotIn("90—180", brief)
        dialog.mode.setCurrentIndex(1)
        self.assertIn("详细版", dialog.planning_objective())
        self.assertIn("固定JSON", dialog.planning_objective())
        self.document.planning_context["form_options"] = dialog.form_options()
        self.document.validate()
        saved = self.root / "form-project.localcut.json"
        save_project(self.document, saved)
        with patched_probe():
            self.assertEqual(load_project(saved).document.planning_context["form_options"], dialog.form_options())
        restored = PlanningOptionsDialog(self.document)
        self.assertEqual(restored.form_options(), dialog.form_options())
        restored.close()
        dialog.close()

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        make_empty_sources(self.root)
        with patched_probe():
            self.document = import_manifest(write_manifest(self.root), self.root)
        self.before = copy.deepcopy(self.document.to_dict())
        self.dialog = ApiPlanningDialog(self.document, [TranscriptCue("19.mp4", 0, 1_000_000, "依据台词")])
        self.dialog.key.setText("unit-test-secret")

    def tearDown(self):
        if self.dialog.worker is not None:
            self.dialog.stop()
            self.wait(lambda: self.dialog.worker is None)
        self.dialog.reject()
        self.app.processEvents()
        self.temporary.cleanup()

    def wait(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt worker did not finish within test timeout")

    def test_optional_open_and_preview_send_nothing_and_cancel_confirmation(self):
        with patch("local_slice_assistant.ai_planning_dialog.request_completion") as send, \
             patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            self.dialog.prepare_payload()
            self.dialog.submit("design")
            send.assert_not_called()
        self.assertIsNone(self.dialog.worker)
        self.assertNotIn("unit-test-secret", self.dialog.payload.toPlainText())
        self.assertNotIn(str(self.root), self.dialog.payload.toPlainText())
        self.assertFalse(self.dialog.plan_button.isEnabled())
        self.assertEqual(self.document.to_dict(), self.before)
        self.dialog.reject()
        self.assertEqual(self.dialog.key.text(), "")

    def test_capacity_preview_and_over_limit_plan_never_requests_or_confirms(self):
        self.dialog.prepare_payload()
        self.assertIn('4 个视频', self.dialog.capacity.text())
        self.assertIn('1 条台词', self.dialog.capacity.text())
        self.dialog.design.setPlainText('长' * 1_500_000)
        with patch('local_slice_assistant.ai_planning_dialog.request_completion') as send, \
             patch('local_slice_assistant.ai_planning_dialog.QMessageBox.question') as confirm:
            self.dialog.submit('plan')
            confirm.assert_not_called()
            send.assert_not_called()
        self.assertIn('超过请求大小上限', self.dialog.status.text())
        self.assertIn('长文本提醒', self.dialog.capacity.text())
        self.assertEqual(len(self.dialog.design.toPlainText()), 1_500_000)
        self.assertIsNone(self.dialog.worker)

    def test_two_explicit_requests_validate_narration_then_review(self):
        calls = []
        def provider(config, messages, *, cancel_event):
            calls.append(messages)
            if len(calls) == 1:
                return "# 设计稿\n先钩子再补前因。解说从成片1秒开始。"
            raw = standard_manifest()
            raw["planning_package_id"] = self.dialog.package["package_id"]
            raw["cuts"][0]["narration"] = {"schema_version": 1, "time_basis": "output", "cues": [
                {"id": "n1", "text": "测试解说", "start_ms": 1000, "end_ms": 3500,
                 "original_audio": "remove_dialogue", "background_gain_db": -12}]}
            return "已确认。\n```json\n" + json.dumps(raw, ensure_ascii=False) + "\n```"
        self.dialog.narration.setChecked(True)
        with patched_probe(), patch("local_slice_assistant.ai_planning_dialog.request_completion", side_effect=provider), \
             patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog.submit("design")
            self.wait(lambda: self.dialog.worker is None)
            self.assertIsNone(self.dialog.candidate)
            self.assertTrue(self.dialog.plan_button.isEnabled())
            self.dialog.design.appendPlainText("我的修订：保留气口。")
            self.dialog.submit("plan")
            self.wait(lambda: self.dialog.worker is None)
        self.assertEqual(len(calls), 2)
        self.assertIn("我的修订", calls[1][2]["content"])
        self.assertNotIn("unit-test-secret", json.dumps(calls))
        self.assertIsNotNone(self.dialog.candidate, self.dialog.status.text())
        self.assertTrue(self.dialog.plan_path.exists())
        self.assertEqual(self.document.to_dict(), self.before)
        candidate = self.dialog.candidate
        self.assertEqual(candidate.active_cut.packaging["narration_plan"]["cues"][0]["start_us"], 1_000_000)
        with patched_probe():
            reopened = load_project(save_project(candidate, self.root / "API工程.localcut.json")).document
        self.assertTrue(reopened.planning_context["include_narration"])
        review = PlanReviewDialog(candidate)
        self.assertEqual(review.narration_table.rowCount(), 1)
        self.assertEqual(review.narration_table.item(0, 0).text(), "00:00:01.000")
        review.reject()
        self.dialog.accept()
        self.assertIs(self.dialog.candidate, candidate)
        self.assertEqual(self.dialog.key.text(), "")
        self.assertNotIn("unit-test-secret", self.dialog.plan_path.read_text(encoding="utf-8"))

    def test_cancel_close_waits_for_worker_without_accepting_late_reply(self):
        def provider(config, messages, *, cancel_event):
            cancel_event.wait(2)
            return "迟到设计稿"
        with patch("local_slice_assistant.ai_planning_dialog.request_completion", side_effect=provider), \
             patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog.submit("design")
            self.dialog.reject()
            self.wait(lambda: self.dialog.worker is None)
        self.assertEqual(self.dialog.design.toPlainText(), "")
        self.assertIsNone(self.dialog.candidate)
        self.assertIn("计费", self.dialog.status.text())
        self.assertEqual(self.document.to_dict(), self.before)

    def test_editing_goal_or_design_invalidates_old_candidate(self):
        self.dialog.prepare_payload()
        original_id = self.dialog.package["package_id"]
        self.dialog.design.setPlainText("已生成设计")
        self.dialog.candidate = self.document
        self.dialog.design.appendPlainText("修改")
        self.assertIsNone(self.dialog.candidate)
        self.dialog.objective.setPlainText("另一个目标")
        self.assertIsNone(self.dialog.package)
        self.assertEqual(self.dialog.design.toPlainText(), "")
        self.dialog.prepare_payload()
        self.assertNotEqual(original_id, self.dialog.package["package_id"])

    def test_error_retains_design_and_redacts_secret_without_retry(self):
        with patch("local_slice_assistant.ai_planning_dialog.request_completion", side_effect=ValueError("unit-test-secret 测试失败")) as send, \
             patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog.prepare_payload()
            self.dialog.design.setPlainText("保留此设计")
            self.dialog.submit("plan")
            self.wait(lambda: self.dialog.worker is None)
        self.assertEqual(send.call_count, 1)
        self.assertNotIn("unit-test-secret", self.dialog.status.text())
        self.assertIn("本次未完成", self.dialog.status.text())
        self.assertEqual(self.dialog.design.toPlainText(), "保留此设计")
        self.assertIsNone(self.dialog.candidate)

    def test_stale_or_rejected_review_never_replaces_project(self):
        window = MainWindow()
        window.document = self.document
        try:
            with patch("local_slice_assistant.gui.PlanReviewDialog.exec", return_value=0) as review:
                window._review_planned_document(copy.deepcopy(self.document), self.document, self.document.revision - 1)
                review.assert_not_called()
                window._review_planned_document(copy.deepcopy(self.document), self.document, self.document.revision)
                review.assert_called_once()
            self.assertIs(window.document, self.document)
            self.assertEqual(self.document.to_dict(), self.before)
        finally:
            window._set_project_baseline(saved=True)
            window.close()
