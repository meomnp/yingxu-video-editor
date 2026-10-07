import copy
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox, QDialog, QPushButton, QPlainTextEdit, QTableWidget

from local_slice_assistant.ai_planning_dialog import ApiPlanningDialog, PlanningOptionsDialog
from local_slice_assistant.gui import MainWindow
from local_slice_assistant.manifest import import_manifest
from local_slice_assistant.plan_review import PlanReviewDialog
from local_slice_assistant.project_store import load_project, save_project
from local_slice_assistant.transcripts import TranscriptCue
from tests.helpers import make_empty_sources, patched_probe, standard_manifest, write_manifest
from local_slice_assistant.batches import safe_name


class ApiPlanningDialogTests(unittest.TestCase):
    def test_api_history_shows_full_saved_path_and_opens_response(self):
        from PySide6.QtGui import QDesktopServices
        from local_slice_assistant.api_artifacts import api_artifact_directory

        batch = self.root.parent / "映序项目" / safe_name(self.root.name) / "测试_第001批"
        batch.mkdir(parents=True)
        response = api_artifact_directory(
            self.document.media_root, create=True, batch_directory=batch
        ) / "合成示例_API设计稿.md"
        response.write_text("完整设计稿", encoding="utf-8")
        dialog = ApiPlanningDialog(self.document, [], parent=None)
        dialog.api_history_path = self.root / "API请求历史.json"
        dialog.api_history = [{
            "created_at": "2026-10-07T12:00:00", "stage": "design", "cut_count": 1,
            "status": "已收到完整设计稿", "saved_file": response.name, "saved_path": str(response),
        }]
        opened = []

        def inspect_history(history_dialog):
            table = history_dialog.findChild(QTableWidget)
            self.assertEqual(table.columnCount(), 7)
            self.assertEqual(table.item(0, 6).text(), str(response))
            table.selectRow(0)
            self.app.processEvents()
            open_file = next(button for button in history_dialog.findChildren(QPushButton)
                             if button.text() == "打开选中回复/方案")
            self.assertTrue(open_file.isEnabled())
            open_file.click()
            history_dialog.accept()

        with patch.object(QDialog, "exec", inspect_history), \
                patch.object(QDesktopServices, "openUrl", side_effect=lambda url: opened.append(url) or True):
            dialog.show_api_history()
        self.assertEqual(Path(opened[0].toLocalFile()), response)
        dialog.close()

    def test_api_history_deletes_one_index_but_keeps_saved_reply(self):
        from local_slice_assistant.api_artifacts import api_artifact_directory

        batch = self.root.parent / "映序项目" / safe_name(self.root.name) / "测试_第001批"
        batch.mkdir(parents=True)
        response = api_artifact_directory(
            self.document.media_root, create=True, batch_directory=batch
        ) / "保留的设计.md"
        response.write_text("保留稿件", encoding="utf-8")
        history_path = self.root / "API请求历史.json"
        dialog = ApiPlanningDialog(self.document, [], parent=None)
        dialog.api_history_path = history_path
        dialog.api_history = [{
            "request_id": "delete-me", "created_at": "2026-10-07T12:00:00",
            "stage": "design", "cut_count": 1, "status": "已收到完整设计稿",
            "saved_file": response.name, "saved_path": str(response),
        }]

        def delete_selected(history_dialog):
            table = history_dialog.findChild(QTableWidget)
            table.selectRow(0)
            button = next(item for item in history_dialog.findChildren(QPushButton)
                          if item.text() == "删除选中记录")
            self.assertTrue(button.isEnabled())
            button.click()

        with patch.object(QDialog, "exec", delete_selected), \
                patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question",
                      return_value=QMessageBox.StandardButton.Yes):
            dialog.show_api_history()
        self.assertEqual(dialog.api_history, [])
        self.assertTrue(response.is_file())
        self.assertEqual(json.loads(history_path.read_text(encoding="utf-8")), [])
        dialog.close()

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

    def test_deepseek_thinking_mode_is_explicit_low_and_custom_api_does_not_get_it(self):
        self.assertEqual(self.dialog.thinking_mode.currentData(), "low")
        self.assertTrue(self.dialog.thinking_mode.isEnabled())
        self.dialog.base_url.setText("https://api.example.com")
        self.assertFalse(self.dialog.thinking_mode.isEnabled())
        self.dialog.base_url.setText("https://api.deepseek.com")
        self.assertTrue(self.dialog.thinking_mode.isEnabled())

    def test_background_wait_does_not_cancel_or_send_again(self):
        from types import SimpleNamespace
        worker = SimpleNamespace(cancel=Event())
        self.dialog.worker = worker
        self.dialog.refresh_buttons()
        with patch.object(self.dialog, 'showMinimized') as minimized:
            # Reconnect the action to the mocked slot for deterministic verification.
            self.dialog.background_button.clicked.disconnect()
            self.dialog.background_button.clicked.connect(self.dialog.showMinimized)
            self.dialog.background_button.click()
            minimized.assert_called_once()
        self.assertIs(self.dialog.worker, worker)
        self.assertFalse(worker.cancel.is_set())
        self.dialog.worker = None

    def test_stream_checkpoint_and_transport_settings_survive_locally(self):
        from types import SimpleNamespace
        self.dialog.wait_minutes.setValue(120)
        self.assertTrue(self.dialog.streaming.isChecked())
        self.assertTrue(self.dialog.prepare_payload())
        self.dialog.document.validate()
        self.dialog.wait_minutes.setValue(60)
        self.assertTrue(self.dialog.prepare_payload())
        self.assertEqual(self.dialog.document.planning_context['api_transport']['wait_minutes'], 60)
        restored = ApiPlanningDialog(self.dialog.document, [])
        self.assertEqual(restored.wait_minutes.value(), 60)
        restored.close()
        self.dialog.worker = SimpleNamespace(cancel=Event())
        self.dialog.api_request_id = 'stream-checkpoint-test'
        self.dialog.receive_stream_progress('design', {'content': '已经收到的部分正文', 'phase': '接收中'})
        self.assertIn('已经收到的部分正文', self.dialog._stream_path.read_text(encoding='utf-8'))
        self.assertEqual(self.dialog.design.toPlainText(), '')
        self.assertTrue(self.dialog.view_raw_reply_button.isEnabled())
        self.dialog.worker = None

    def test_api_test_allows_ten_cuts_and_reflects_count_in_request_package(self):
        self.assertEqual(self.dialog.windowTitle(), "API 剪辑设计")
        self.assertIn("产生费用", self.dialog.status.text())
        self.assertNotIn("实验功能", self.dialog.status.text())
        self.assertEqual(self.dialog.cut_count.maximum(), 10)
        self.dialog.cut_count.setValue(10)
        self.assertTrue(self.dialog.prepare_payload())
        self.assertEqual(self.dialog.package["requested_cut_counts"]["total"], 10)
        self.assertIn("设计 10 条", self.dialog.package["planning_request"]["objective"])
        self.assertIn("每组对应一条独立成片", self.dialog.package["web_gpt_design_prompt"])

    def test_api_dialog_selects_exact_narrated_cut_count(self):
        self.dialog.cut_count.setValue(10)
        self.dialog.narration_count.setValue(3)
        self.assertIn("3 条带解说，7 条保留原声", self.dialog.narration_summary.text())
        self.assertTrue(self.dialog.prepare_payload())
        self.assertEqual(self.dialog.package["requested_cut_counts"]["total"], 10)
        self.assertEqual(self.dialog.package["requested_cut_counts"]["narrated"], 3)
        self.assertTrue(self.dialog.package["planning_request"]["include_narration"])
        self.assertIn("原声 7 条、带解说 3 条", self.dialog.package["planning_request"]["objective"])
        self.dialog.cut_count.setValue(2)
        self.assertEqual(self.dialog.narration_count.value(), 2)

    def test_api_and_external_export_compose_identical_structured_goals(self):
        exported = PlanningOptionsDialog(self.document)
        exported.count.setValue(10)
        exported.narration_count.setValue(3)
        exported.duration.setText("2—3 分钟")
        exported.direction.setText("人物反击")
        exported.mode.setCurrentIndex(1)
        exported.objective.setPlainText("保留反应镜头")
        self.dialog.cut_count.setValue(10)
        self.dialog.narration_count.setValue(3)
        self.dialog.duration.setText("2—3 分钟")
        self.dialog.direction.setText("人物反击")
        self.dialog.mode.setCurrentIndex(1)
        self.dialog.objective.setPlainText("保留反应镜头")
        self.assertIn("每条目标时长：2—3 分钟", self.dialog.narration_summary.text())
        self.assertTrue(self.dialog.prepare_payload())
        self.assertEqual(self.dialog.package["planning_request"]["objective"], exported.planning_objective())
        self.assertEqual(self.dialog.document.planning_context["form_options"], exported.form_options())
        restored = ApiPlanningDialog(self.dialog.document, self.dialog.cues)
        self.assertEqual(restored.duration.text(), "2—3 分钟")
        self.assertEqual(restored.objective.toPlainText(), "保留反应镜头")
        restored.close()
        exported.close()

    def test_blank_api_duration_prevents_request_and_duration_change_invalidates_old_goal(self):
        self.assertTrue(self.dialog.prepare_payload())
        self.dialog.duration.clear()
        self.assertIsNone(self.dialog.package)
        with patch("local_slice_assistant.ai_planning_dialog.request_completion") as send:
            self.dialog.submit("design")
            send.assert_not_called()
        self.assertIn("请填写每条成片的目标时长", self.dialog.status.text())
        self.dialog.duration.setText("45 秒左右")
        self.assertTrue(self.dialog.prepare_payload())
        self.assertIn("每条目标时长：45 秒左右", self.dialog.package["planning_request"]["objective"])

    def test_api_uses_same_selected_builtin_or_custom_analysis_template(self):
        self.dialog.template_kind.setCurrentIndex(self.dialog.template_kind.findData("vlog"))
        self.assertTrue(self.dialog.prepare_payload())
        from local_slice_assistant.editing_design_rules import BUILTIN_TEMPLATES
        self.assertIn(BUILTIN_TEMPLATES["vlog"][1], self.dialog.package["web_gpt_design_prompt"])
        self.dialog.template_text = "自定义模板：按时间顺序梳理真实事件，避免戏剧化包装。"
        self.dialog.template_name = "我的模板.md"
        self.dialog.invalidate_package()
        self.assertTrue(self.dialog.prepare_payload())
        prompt = self.dialog.package["web_gpt_design_prompt"]
        self.assertIn(self.dialog.template_text, prompt)
        self.assertIn("固定执行规则｜不受上方模板更改", prompt)
        self.assertEqual(self.dialog.document.planning_context["form_options"]["template_name"], "我的模板.md")
        self.dialog.template_kind.setCurrentIndex(self.dialog.template_kind.findData("talk"))
        self.assertEqual(self.dialog.template_text, "")
        self.assertTrue(self.dialog.prepare_payload())
        self.assertIn(BUILTIN_TEMPLATES["talk"][1], self.dialog.package["web_gpt_design_prompt"])

    def test_load_historical_design_without_request_and_reuse_for_second_stage(self):
        from local_slice_assistant.api_artifacts import api_artifact_directory

        saved = api_artifact_directory(self.document.media_root, create=True) / "历史设计.md"
        saved.write_text("# 历史设计稿\n保留人物反应，再揭示转折。", encoding="utf-8")
        self.dialog.api_history = [{
            "created_at": "2026-10-07T12:00:00", "stage": "design", "drama": self.document.drama,
            "cut_count": 1, "status": "已收到完整设计稿 · 待人工审阅",
            "saved_file": saved.name, "saved_path": str(saved),
        }]
        with patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.Yes), \
                patch("local_slice_assistant.ai_planning_dialog.request_completion") as send:
            self.assertTrue(self.dialog._load_historical_design(self.dialog.api_history[0]))
            send.assert_not_called()
            self.assertIn("历史设计稿", self.dialog.design.toPlainText())
            self.assertFalse(self.dialog.plan_button.isEnabled())
            self.assertIn("1 条独立成片", self.dialog.design_count_confirmed.text())
            self.dialog.design_count_confirmed.setChecked(True)
            self.assertTrue(self.dialog.plan_button.isEnabled())
            messages = self.dialog.request_messages("plan")
            self.assertIn("历史设计稿", messages[2]["content"])
            self.assertIn("我确认上述设计", messages[3]["content"])
        self.dialog.close()

    def test_historical_design_count_mismatch_blocks_second_paid_request(self):
        from local_slice_assistant.api_artifacts import api_artifact_directory

        saved = api_artifact_directory(self.document.media_root, create=True) / "一条历史设计.md"
        saved.write_text("# 一条设计\n只规划一个成片。", encoding="utf-8")
        self.dialog.cut_count.setValue(10)
        self.assertTrue(self.dialog.prepare_payload())
        entry = {
            "created_at": "2026-10-07T12:00:00", "stage": "design", "drama": self.document.drama,
            "cut_count": 1, "status": "已收到完整设计稿 · 待人工审阅",
            "saved_file": saved.name, "saved_path": str(saved),
        }
        with patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.Yes), \
                patch("local_slice_assistant.ai_planning_dialog.request_completion") as send:
            self.assertTrue(self.dialog._load_historical_design(entry))
            self.assertFalse(self.dialog.plan_button.isEnabled())
            self.assertIn("历史设计稿：目标 1 条成片；当前任务要求 10 条成片", self.dialog.status.text())
            self.dialog.submit("plan")
            send.assert_not_called()
        self.dialog.close()

    def test_history_reuse_preserves_request_and_terminal_status(self):
        self.assertTrue(self.dialog.prepare_payload())
        self.dialog.loaded_design_history = {"cut_count": 1, "drama": self.document.drama}
        self.dialog.design.setPlainText("历史完整设计稿")
        self.dialog.design_count_confirmed.setChecked(True)
        with patch("local_slice_assistant.ai_planning_dialog.request_completion",
                   side_effect=RuntimeError("模拟服务失败")), \
                patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question",
                      return_value=QMessageBox.StandardButton.Yes):
            self.dialog.submit("plan")
            self.assertIn("请求已提交", self.dialog.status.text())
            self.wait(lambda: self.dialog.worker is None)
        self.assertIn("模拟服务失败", self.dialog.status.text())
        self.assertNotIn("尚未发送请求", self.dialog.status.text())

    def test_redesign_after_history_count_mismatch_preserves_network_failure(self):
        self.dialog.cut_count.setValue(10)
        self.assertTrue(self.dialog.prepare_payload())
        self.dialog.loaded_design_history = {"cut_count": 1, "drama": self.document.drama}
        self.dialog.design.setPlainText("旧的一条设计稿")
        with patch("local_slice_assistant.ai_planning_dialog.request_completion",
                   side_effect=RuntimeError("重新设计模拟失败")), \
                patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question",
                      return_value=QMessageBox.StandardButton.Yes):
            self.dialog.submit("design")
            self.assertIn("请求已提交", self.dialog.status.text())
            self.wait(lambda: self.dialog.worker is None)
        self.assertIn("重新设计模拟失败", self.dialog.status.text())
        self.assertFalse(self.dialog.plan_button.isEnabled())

    def test_history_reuse_only_accepts_complete_markdown_design(self):
        from local_slice_assistant.api_artifacts import api_artifact_directory
        from local_slice_assistant.batches import create_batch

        batch = create_batch(self.document)
        self.dialog.document.planning_context["batch"] = {"directory": str(batch)}
        saved = api_artifact_directory(self.document.media_root, create=True, batch_directory=batch) / "方案.json"
        saved.write_text("{}", encoding="utf-8")
        entry = {"stage": "design", "status": "已收到完整设计稿", "saved_path": str(saved)}
        self.assertIsNone(self.dialog._entry_design_path(entry))
        entry["status"] = "未完成回复 · 不可导入"
        self.assertIsNone(self.dialog._entry_design_path(entry))
        entry = {
            "stage": "design", "status": "已收到完整设计稿 · 待人工审阅",
            "saved_file": "合成示例_API设计稿.md",
        }
        legacy_path = api_artifact_directory(
            self.document.media_root, create=True, batch_directory=batch
        ) / entry["saved_file"]
        legacy_path.write_text("旧记录格式", encoding="utf-8")
        self.assertEqual(self.dialog._entry_design_path(entry), legacy_path)

    def test_unparseable_plan_reply_is_saved_and_linked_from_history(self):
        self.dialog.prepare_payload()
        self.dialog.design.setPlainText("完整设计稿，供模型生成 JSON 方案。")
        self.dialog.design_count_confirmed.setChecked(True)
        raw_reply = "这是模型返回的说明文字，但没有 JSON 方案。"
        with patch("local_slice_assistant.ai_planning_dialog.request_completion", return_value=raw_reply), \
                patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question",
                      return_value=QMessageBox.StandardButton.Yes):
            self.dialog.submit("plan")
            self.wait(lambda: self.dialog.worker is None)
        entry = self.dialog.api_history[-1]
        saved = Path(entry["saved_path"])
        self.assertEqual(entry["status"], "失败 / 未获得可用完整结果")
        self.assertEqual(entry["raw_reply_path"], str(saved))
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.suffix, ".md")
        self.assertIn(raw_reply, saved.read_text(encoding="utf-8"))
        self.assertIn(str(saved), self.dialog.status.text())

    def test_plan_reply_disk_write_failure_remains_visible_and_copyable_in_window(self):
        from types import SimpleNamespace
        raw_reply = "模型回复正文：十条方案如下……"
        self.dialog.worker = SimpleNamespace(cancel=Event())
        with patch("local_slice_assistant.ai_planning_dialog.api_artifact_directory",
                   side_effect=OSError("磁盘空间不足")):
            self.dialog.save_plan_reply("plan", raw_reply)
        self.assertEqual(self.dialog._last_plan_reply_content, raw_reply)
        self.assertIsNone(self.dialog._last_plan_reply_path)
        self.assertTrue(self.dialog.view_raw_reply_button.isEnabled())
        self.dialog.failure("回复未能解码成可导入方案")
        self.assertIn("仍暂存在本窗口", self.dialog.status.text())
        copied = []

        def inspect_raw_reply(raw_dialog):
            text = raw_dialog.findChild(QPlainTextEdit)
            self.assertTrue(text.isReadOnly())
            self.assertEqual(text.toPlainText(), raw_reply)
            copy_button = next(button for button in raw_dialog.findChildren(QPushButton)
                               if button.text() == "复制全文")
            copy_button.click()
            copied.append(QApplication.clipboard().text())
            raw_dialog.accept()

        with patch.object(QDialog, "exec", inspect_raw_reply):
            self.dialog.show_raw_reply()
        self.assertEqual(copied, [raw_reply])
        self.dialog.worker = None

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
        self.dialog.design_count_confirmed.setChecked(True)
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
        configs = []
        def provider(config, messages, *, cancel_event):
            calls.append(messages)
            configs.append(config)
            if len(calls) == 1:
                return "# 设计稿\n先钩子再补前因。解说从成片1秒开始。"
            raw = standard_manifest()
            raw["planning_package_id"] = self.dialog.package["package_id"]
            raw["cuts"][0]["narration"] = {"schema_version": 1, "time_basis": "output", "cues": [
                {"id": "n1", "text": "测试解说", "start_ms": 1000, "end_ms": 3500,
                 "original_audio": "remove_dialogue", "background_gain_db": -12}]}
            return "已确认。\n```json\n" + json.dumps(raw, ensure_ascii=False) + "\n```"
        self.dialog.narration_count.setValue(1)
        with patched_probe(), patch("local_slice_assistant.ai_planning_dialog.request_completion", side_effect=provider), \
             patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog.submit("design")
            self.wait(lambda: self.dialog.worker is None)
            self.assertIsNone(self.dialog.candidate)
            self.assertIs(self.dialog.tabs.currentWidget(), self.dialog.design.parentWidget())
            self.assertFalse(self.dialog.plan_button.isEnabled())
            self.dialog.design_count_confirmed.setChecked(True)
            self.assertTrue(self.dialog.plan_button.isEnabled())
            self.dialog.design.appendPlainText("我的修订：保留气口。")
            self.assertFalse(self.dialog.design_count_confirmed.isChecked())
            self.dialog.design_count_confirmed.setChecked(True)
            self.dialog.submit("plan")
            self.wait(lambda: self.dialog.worker is None)
        self.assertEqual(len(calls), 2)
        self.assertEqual([config.max_output_tokens for config in configs], [393216, 393216])
        self.assertEqual([config.thinking_mode for config in configs], ["low", "low"])
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

    def test_ten_cut_api_plan_import_uses_current_request_count_not_saved_project_count(self):
        calls = []

        def provider(config, messages, *, cancel_event):
            calls.append(messages)
            if len(calls) == 1:
                return "# 设计稿\n成片01至成片10分别设计，每条均为独立成片。"
            raw = standard_manifest()
            raw["planning_package_id"] = self.dialog.package["package_id"]
            prototype = copy.deepcopy(raw["cuts"][0])
            raw["cuts"] = [dict(copy.deepcopy(prototype), title=f"测试成片{i + 1:02d}") for i in range(10)]
            return "```json\n" + json.dumps(raw, ensure_ascii=False) + "\n```"

        # Simulate a project whose saved goal was one cut, while the user
        # explicitly changes this API request to ten.
        self.document.planning_context["form_options"] = {"count": 1, "narration_count": 0}
        self.dialog.cut_count.setValue(10)
        self.assertTrue(self.dialog.prepare_payload())
        self.assertEqual(self.dialog.package["requested_cut_counts"]["total"], 10)
        self.dialog.design_count_confirmed.setChecked(True)
        with patched_probe(), patch("local_slice_assistant.ai_planning_dialog.request_completion", side_effect=provider), \
                patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog.submit("design")
            self.wait(lambda: self.dialog.worker is None)
            self.assertIn("已收到完整设计稿", self.dialog.status.text())
            self.dialog.design_count_confirmed.setChecked(True)
            self.dialog.submit("plan")
            self.wait(lambda: self.dialog.worker is None)

        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(self.dialog.candidate, self.dialog.status.text())
        self.assertEqual(len(self.dialog.candidate.cuts), 10)
        self.assertEqual(self.dialog.candidate.planning_context["form_options"]["count"], 10)
        self.assertEqual(self.document.planning_context["form_options"]["count"], 1)

    def test_real_local_http_two_stages_preserve_usage_and_save_ten_cuts(self):
        from tests.test_ai_provider import local_server, response_json

        def reply(request):
            package = json.loads(request["messages"][1]["content"].rsplit("\n请执行 web_gpt_design_prompt。", 1)[0])
            if len(request["messages"]) == 2:
                content = "# 本机模拟设计稿\n成片01至成片10，各为独立成片。"
            else:
                raw = standard_manifest()
                raw["planning_package_id"] = package["package_id"]
                prototype = copy.deepcopy(raw["cuts"][0])
                raw["cuts"] = [dict(copy.deepcopy(prototype), title=f"成片{i + 1:02d}") for i in range(10)]
                content = json.dumps(raw, ensure_ascii=False)
            envelope = json.loads(response_json(content))
            envelope.update(id="local-test-response", model="local-simulated-model",
                            usage={"prompt_tokens": 321, "completion_tokens": 654, "total_tokens": 975})
            return json.dumps(envelope, ensure_ascii=False).encode("utf-8")

        with local_server(data=reply) as (url, records, _), patched_probe(), \
                patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question",
                      return_value=QMessageBox.StandardButton.Yes):
            self.dialog.base_url.setText(url + "/v1")
            self.dialog.cut_count.setValue(10)
            self.dialog.submit("design")
            self.wait(lambda: self.dialog.worker is None)
            self.assertIn("已收到完整设计稿", self.dialog.status.text())
            self.assertTrue(Path(self.dialog.api_history[-1]["saved_path"]).is_file())
            self.dialog.design_count_confirmed.setChecked(True)
            self.dialog.submit("plan")
            self.wait(lambda: self.dialog.worker is None)
            self.assertIsNotNone(self.dialog.candidate, self.dialog.status.text())
            self.assertEqual(len(self.dialog.candidate.cuts), 10)
            self.assertEqual(len(records), 2)
            self.assertEqual([record["path"] for record in records], ["/v1/chat/completions"] * 2)
            for entry in self.dialog.api_history[-2:]:
                self.assertEqual(entry["usage"]["prompt_tokens"], 321)
                self.assertEqual(entry["usage"]["completion_tokens"], 654)
                self.assertEqual(entry["finish_reason"], "stop")
            disk_history = json.loads(self.dialog.api_history_path.read_text(encoding="utf-8"))
            self.assertEqual(disk_history[-1]["usage"]["total_tokens"], 975)
            self.assertTrue(Path(disk_history[-1]["raw_reply_path"]).is_file())
            restored = load_project(save_project(self.dialog.candidate, self.root / "本机HTTP工程.localcut.json"))
            self.assertEqual(len(restored.document.cuts), 10)
        self.assertEqual(self.document.to_dict(), self.before)

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

    def test_duplicate_submission_while_busy_never_sends_another_request(self):
        def provider(config, messages, *, cancel_event):
            cancel_event.wait(3)
            return "停止后迟到的正文"

        for stage in ("design", "plan"):
            with self.subTest(stage=stage):
                self.dialog.prepare_payload()
                if stage == "plan":
                    self.dialog.design.setPlainText("已核对的一条设计稿")
                    self.dialog.design_count_confirmed.setChecked(True)
                before_history = len(self.dialog.api_history)
                with patch("local_slice_assistant.ai_planning_dialog.request_completion", side_effect=provider) as send, \
                        patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as confirmation:
                    self.dialog.submit(stage)
                    worker = self.dialog.worker
                    self.assertIsNotNone(worker)
                    self.wait(lambda: send.call_count == 1)
                    active_status = self.dialog.status.text()
                    for _ in range(3):
                        self.dialog.submit(stage)
                    self.assertIs(self.dialog.worker, worker)
                    self.assertEqual(send.call_count, 1)
                    self.assertEqual(confirmation.call_count, 1)
                    self.assertEqual(len(self.dialog.api_history), before_history + 1)
                    self.assertEqual(self.dialog.status.text(), active_status)
                    self.dialog.stop()
                    self.wait(lambda: self.dialog.worker is None)
                self.assertIsNone(self.dialog.candidate)
        self.assertEqual(self.document.to_dict(), self.before)

    def test_changed_task_fingerprint_is_visible_before_loading_historical_design(self):
        from local_slice_assistant.api_artifacts import api_artifact_directory
        self.dialog.prepare_payload()
        saved_hash = self.dialog._design_context_fingerprint()
        saved = api_artifact_directory(self.document.media_root, create=True) / "历史上下文.md"
        saved.write_text("旧目标的一条历史设计", encoding="utf-8")
        entry = dict(stage="design", cut_count=1, saved_path=str(saved), saved_file=saved.name,
                     status="已收到完整设计稿", design_context_hash=saved_hash)
        self.dialog.duration.setText("2—3 分钟")
        with patch("local_slice_assistant.ai_planning_dialog.request_completion") as send, \
                patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as confirmation:
            self.assertFalse(self.dialog._load_historical_design(entry))
            self.assertIn("历史任务内容与当前任务不同", confirmation.call_args.args[2])
            self.assertEqual(self.dialog.design.toPlainText(), "")
            self.assertIsNone(self.dialog.loaded_design_history)
            send.assert_not_called()
        with patch("local_slice_assistant.ai_planning_dialog.request_completion") as send, \
                patch("local_slice_assistant.ai_planning_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.assertTrue(self.dialog._load_historical_design(entry))
            self.assertTrue(self.dialog.loaded_design_history["context_mismatch"])
            self.assertFalse(self.dialog.design_count_confirmed.isChecked())
            self.assertFalse(self.dialog.plan_button.isEnabled())
            send.assert_not_called()

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
            self.dialog.design_count_confirmed.setChecked(True)
            self.dialog.submit("plan")
            self.wait(lambda: self.dialog.worker is None)
        self.assertEqual(send.call_count, 1)
        self.assertNotIn("unit-test-secret", self.dialog.status.text())
        self.assertIn("未获得可用的完整结果", self.dialog.status.text())
        self.assertIn("不会自动重试", self.dialog.status.text())
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
