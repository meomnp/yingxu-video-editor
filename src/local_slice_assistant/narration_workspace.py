"""One dedicated workspace for timed narration, with persisted recovery."""
from copy import deepcopy
import json
from pathlib import Path

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView, QAbstractItemView)

from .narration_plan import load_narration, timeline_fingerprint
from .narration_batch import create_batch, retry_failed_job
from .narration_pipeline import load_batch, produce_narration
from .timeline import build_render_timeline, total_duration_us
from .voice_profiles_ui import VoiceProfilePicker


def design_task(cut):
    timeline = []
    for item in build_render_timeline(cut):
        segment = item.segment
        timeline.append(dict(output_start_ms=item.output_in_us // 1000,
            output_end_ms=item.output_out_us // 1000, kind=item.kind,
            source_file=segment.source_file if segment else None,
            source_in_ms=segment.in_us // 1000 if segment else None,
            source_out_ms=segment.out_us // 1000 if segment else None,
            purpose=segment.purpose if segment else None))
    example = dict(schema_version=1, time_basis="output", timeline_fingerprint=timeline_fingerprint(cut),
        cues=[dict(id="n1", start_ms=0, end_ms=min(5000, total_duration_us(cut)//1000),
            text="替换为与实际画面相符的简短解说", original_audio="remove_dialogue", background_gain_db=-12)])
    return ("# 当前成片解说设计任务\n\n"
        "请根据用户提供的视频或台词设计解说。下表仅说明剪辑位置，不能据此虚构剧情；信息不足先询问。"
        "输出一个 JSON 文件，不加 Markdown 围栏。保留 time_basis 和 timeline_fingerprint。"
        "所有 start_ms/end_ms 是成片时间，不是原素材时间。每句不超过300字，时间窗不能重叠或越界；"
        "按自然语速写短句并留停顿，不用塞满时间。保留关键对白的地方不要安排解说。"
        "remove_dialogue 表示去原人声并降低背景，keep 表示保留原声并按音量值降低，mute 是明确静音。"
        "默认 remove_dialogue、-12 dB。不要更改剪辑顺序；若要改剪请先另行提供剪辑清单。\n\n"
        f"成片总长：{total_duration_us(cut)//1000} 毫秒\n\n"
        "时间映射：\n" + json.dumps(timeline, ensure_ascii=False, indent=2) +
        "\n\n输出结构示例（解说内容和时间需要设计）：\n" + json.dumps(example, ensure_ascii=False, indent=2))


class NarrationWorkspace(QDialog):
    status_changed = Signal(str)

    def __init__(self, host):
        super().__init__(host)
        self.host, self.document = host, host.document
        self.cut_id = host._current_cut_id()
        self.running = False
        self.cancel_event = None
        self.stop_requested = False
        self.setWindowTitle("整段解说制作 · 本地运行")
        self.resize(900, 580)
        layout = QVBoxLayout(self)
        intro = QLabel("先固定剪辑顺序，再导入成片时间的解说稿。只在解说时间窗去原人声并压低背景；窗外保留原声。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.design_button = QPushButton("1 导出 AI 设计任务")
        self.import_button = QPushButton("2 导入解说稿")
        for button, action in ((self.design_button, self.export_task), (self.import_button, self.import_script)):
            button.clicked.connect(action)
            row.addWidget(button)
        layout.addLayout(row)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["成片开始（秒）", "成片结束（秒）", "解说词", "原声处理", "制作状态"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.status = QLabel("导入 JSON / SRT / Markdown / TXT 解说稿后，会在这里显示每句时间和状态。")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_changed.connect(self.status.setText)
        layout.addWidget(self.status)
        self.profile_picker = VoiceProfilePicker(self)
        layout.addWidget(self.profile_picker)
        note = QLabel("新批次使用上方选择的音色，请先启动本地声音工作台。分离在本机 CPU 运行，可能较慢。停止配音等待不保证模型停止；继续会找回原任务，不重复提交。")
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.start_button = QPushButton("3 开始 / 继续制作")
        self.retry_button = QPushButton("重试选中失败句")
        self.stop_button = QPushButton("停止")
        self.preview_button = QPushButton("4 预览成片")
        close = QPushButton("关闭")
        for button, action in ((self.start_button, self.start), (self.retry_button, self.retry),
                (self.stop_button, self.stop), (self.preview_button, self.preview), (close, self.reject)):
            button.clicked.connect(action)
            row.addWidget(button)
        layout.addLayout(row)
        self.profile_picker.changed.connect(self._update_start)
        self.refresh()

    def cut(self):
        if self.host.document is not self.document:
            raise ValueError("工程已切换，请重新打开解说工作台。")
        return self.document.get_cut(self.cut_id)

    def refresh(self):
        cut = self.cut()
        plan = cut.packaging.get('narration_plan', {})
        jobs = {}
        path = cut.packaging.get('narration_batch_path')
        if path:
            try:
                batch = load_batch(path, cut, plan)
                jobs = {job['cue_id']: job for job in batch['jobs']}
                self.profile_picker.bind(batch['profile_id'], batch.get('profile_snapshot'))
            except (OSError, ValueError, KeyError) as exc:
                self.status.setText(str(exc))
        else:
            self.profile_picker.unlock()
        cues = plan.get('cues', [])
        self.table.setRowCount(len(cues))
        states = dict(pending='待制作', submitted='已提交', waiting='待找回', verifying='校验中', ready='已完成', failed='失败')
        modes = dict(remove_dialogue='去人声、留背景', keep='保留并压低原声', mute='静音')
        for row, cue in enumerate(cues):
            job = jobs.get(cue['id'], {})
            values = [f"{cue['start_us']/1e6:.3f}", f"{cue['end_us']/1e6:.3f}", cue['text'],
                modes[cue['original_audio']], states.get(job.get('state'), '待制作')]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(job.get('error', text) if col == 4 else text)
                self.table.setItem(row, col, item)
        self.profile_picker.set_busy(self.running)
        self._update_start()
        self.retry_button.setEnabled(bool(jobs) and not self.running)
        self.stop_button.setEnabled(self.running)
        self.import_button.setEnabled(not self.running)
        self.preview_button.setEnabled(bool(cut.packaging.get('narration_render')) and not self.running)

    def _update_start(self):
        if self.host.document is not self.document:
            self.start_button.setEnabled(False)
            return
        self.start_button.setEnabled(bool(self.cut().packaging.get('narration_plan', {}).get('cues'))
            and not self.running and (self.profile_picker.locked or not self.profile_picker.loading))

    def export_task(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出设计任务", "解说设计任务.md", "Markdown (*.md)")
        if path:
            try:
                Path(path).write_text(design_task(self.cut()), encoding='utf-8')
                self.status.setText("设计任务已导出。连同对应视频或台词交给 AI，再导回其 JSON 解说稿。")
            except (OSError, ValueError) as exc:
                self.status.setText(str(exc))

    def import_script(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入成片时间的解说稿", "", "解说稿 (*.json *.srt *.md *.markdown *.txt)")
        if not path:
            return
        try:
            if QMessageBox.question(self, "确认解说时间", "稿件时间必须对应当前成片，不是原素材时间。导入会替换当前解说计划（可撤销），旧制作记录保留在磁盘。\n确认导入并检查下方每句时间？") != QMessageBox.StandardButton.Yes:
                return
            plan = load_narration(path, self.cut(), confirm_output_time=True)
            packaging = deepcopy(self.cut().packaging)
            packaging['narration_plan'] = plan
            for key in ('narration_batch_path', 'narration_render'):
                packaging.pop(key, None)
            self.document.replace_packaging(self.cut_id, packaging, "导入整段解说稿")
            self.refresh()
            self.status.setText("解说稿已导入，请核对时间和台词；确认后点击开始制作。此时尚未调用配音。")
        except Exception as exc:
            self.status.setText(f"未导入：{exc}")

    def start(self):
        if self.running:
            return
        if self.host._active_worker or self.host._task_queue:
            self.status.setText("其他任务仍在运行，请待其完成后再开始解说制作。")
            return
        try:
            cut = self.cut()
            plan = cut.packaging['narration_plan']
            path = cut.packaging.get('narration_batch_path')
            if path:
                batch = load_batch(path, cut, plan)
            else:
                if self.profile_picker.loading:
                    self.status.setText("正在读取本地音色，完成后即可制作。")
                    return
                profile = self.profile_picker.selected_profile()
                batch = create_batch(plan, cut, Path(self.document.media_root) / '.local_slice_assistant' / 'narration',
                                     profile['id'], profile_snapshot=profile)
                packaging = deepcopy(cut.packaging)
                packaging['narration_batch_path'] = str(Path(batch['directory']) / 'batch.json')
                self.document.replace_packaging(self.cut_id, packaging, "创建整段解说批次")
            snapshot = self.host._snapshot(self.document)
            revision = self.document.revision
            self.running = True
            self.stop_requested = False
            self.refresh()
            def task(progress, cancel):
                self.cancel_event = cancel
                if self.stop_requested:
                    cancel.set()
                def report(message):
                    progress(message)
                    self.status_changed.emit(message)
                return produce_narration(snapshot, self.cut_id, batch, report, cancel)
            def finished(value):
                self.running = False
                self.cancel_event = None
                if self.host.document is not self.document or self.document.revision != revision:
                    self.status.setText("工程已变化，结果仅保存在批次目录，未应用到新时间轴。")
                    return
                if value.get('phase') == '制作完成':
                    packaging = deepcopy(self.cut().packaging)
                    packaging['narration_render'] = value['render']
                    self.document.replace_packaging(self.cut_id, packaging, "应用整段解说混音")
                    self.status.setText("制作完成，可预览；确认后保存工程并导出包装成片。")
                else:
                    self.status.setText("尚未完成。待找回句点击继续；明确失败句选中后重试。失败原因见状态列提示。")
                self.refresh()
            def failed(exc):
                self.running = False
                self.cancel_event = None
                self.refresh()
                self.status.setText(f"制作未完成，记录已保留：{exc}")
            self.host._run_task("整段解说制作", task, finished, failed)
        except Exception as exc:
            self.running = False
            self.status.setText(f"未开始：{exc}")
            self.refresh()

    def retry(self):
        row = self.table.currentRow()
        if row < 0:
            self.status.setText("先选择一条明确失败的解说。")
            return
        try:
            cut = self.cut()
            batch = load_batch(cut.packaging['narration_batch_path'], cut, cut.packaging['narration_plan'])
            retry_failed_job(batch, batch['plan']['cues'][row]['id'])
            self.refresh()
            self.status.setText("选中失败句已准备新任务，点击继续制作；其他成功句不重新生成。")
        except Exception as exc:
            self.status.setText(str(exc))

    def stop(self):
        self.stop_requested = True
        if self.cancel_event:
            self.cancel_event.set()
            self.status.setText("正在停止；已完成结果保留。配音服务器可能仍在生成，请稍后继续找回。")

    def reject(self):
        if self.running:
            self.stop()
        super().reject()

    def preview(self):
        self.accept()
        self.host.preview_packaged()
