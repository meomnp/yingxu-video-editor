"""Unsaved-project protection for a Qt host with the existing background queue.

The host supplies document, project_path, _run_task, _set_status and _error.
Call _set_project_baseline after installing a document, and route replacements
and close requests through _confirm_replace_or_close. Continuations are deferred
until the current result callback returns; this module never waits for a worker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .errors import ExportCancelled
from .models import ProjectDocument
from .project_store import default_project_path, save_project


@dataclass
class _PendingChange:
    document: ProjectDocument | None
    generation: int
    continuation: Callable[[], None]


@dataclass
class _PendingSave:
    document: ProjectDocument
    generation: int
    content: str
    gate: _PendingChange | None = None


class ProjectSessionMixin:
    def _init_project_session(self) -> None:
        self._session_generation = 0
        self._baseline_document: ProjectDocument | None = None
        self._baseline_content: str | None = None
        self._session_gate: _PendingChange | None = None
        self._session_confirmation: QMessageBox | None = None
        self._session_save: _PendingSave | None = None
        self._session_save_dialog_open = False

    @staticmethod
    def _project_content(document: ProjectDocument | None) -> str | None:
        if document is None:
            return None
        return json.dumps(document.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _set_project_baseline(self, *, saved: bool = True) -> None:
        """Call after document/path assignment; imports and recovered files use False."""
        self._session_generation += 1
        self._session_gate = None
        dialog, self._session_confirmation = self._session_confirmation, None
        if dialog is not None:
            dialog.reject()
        self._baseline_document = self.document
        self._baseline_content = self._project_content(self.document) if saved else None
        self._refresh_project_session_state()

    def _project_is_dirty(self) -> bool:
        return self.document is not None and (
            self.document is not self._baseline_document
            or self._project_content(self.document) != self._baseline_content
        )

    def _refresh_project_session_state(self) -> None:
        self.setWindowModified(self._project_is_dirty())

    def _gate_is_current(self, gate: _PendingChange) -> bool:
        return (
            self._session_gate is gate
            and self.document is gate.document
            and self._session_generation == gate.generation
        )

    def _abort_session_gate(self, gate: _PendingChange | None, message: str = "") -> None:
        if gate is not None and self._session_gate is gate:
            self._session_gate = None
            if message:
                self._set_status(message)

    def _continue_session_gate(self, gate: _PendingChange, expected_content: str | None) -> None:
        def continue_when_safe() -> None:
            if not self._gate_is_current(gate):
                return
            if self._project_content(self.document) != expected_content:
                self._abort_session_gate(gate, "工程又有修改，已保留当前编辑；请再次关闭或切换工程。")
                return
            self._session_gate = None
            gate.continuation()

        QTimer.singleShot(0, continue_when_safe)

    def _confirm_replace_or_close(self, continuation: Callable[[], None]) -> None:
        """Run continuation only after a clean state or an explicit discard/save.

        Repeated close/replacement requests share the first pending decision.
        Callers must not execute their action based on this method's return value.
        """
        if self._session_gate is not None:
            self._set_status("请先完成当前的保存或放弃修改操作。")
            return
        gate = _PendingChange(self.document, self._session_generation, continuation)
        self._session_gate = gate
        if not self._project_is_dirty():
            self._continue_session_gate(gate, self._project_content(self.document))
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("工程尚未保存")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setText(f"“{self.document.drama}”有尚未保存的修改。")
        dialog.setInformativeText("保存后继续，或放弃这些修改。取消会保留当前工程。")
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.button(QMessageBox.StandardButton.Save).setText("保存工程")
        dialog.button(QMessageBox.StandardButton.Discard).setText("放弃修改")
        dialog.button(QMessageBox.StandardButton.Cancel).setText("取消")
        self._session_confirmation = dialog

        def decided(result: int) -> None:
            if self._session_confirmation is dialog:
                self._session_confirmation = None
            dialog.deleteLater()
            if not self._gate_is_current(gate):
                return
            if result == QMessageBox.StandardButton.Save.value:
                self._save_project_for_gate(gate)
            elif result == QMessageBox.StandardButton.Discard.value:
                self._continue_session_gate(gate, self._project_content(self.document))
            else:
                self._abort_session_gate(gate, "已取消关闭或切换，当前工程保持不变。")

        dialog.finished.connect(decided)
        dialog.open()

    def save_project_dialog(self) -> None:
        self._save_project_for_gate(None)

    def _save_project_for_gate(self, gate: _PendingChange | None) -> None:
        document = self.document
        if document is None:
            self._abort_session_gate(gate)
            self._set_status("请先导入素材或打开工程。")
            return
        pending = self._session_save
        if pending is not None:
            if (
                gate is not None
                and pending.document is document
                and pending.generation == self._session_generation
                and pending.content == self._project_content(document)
            ):
                pending.gate = gate
                self._set_status("正在保存当前工程，完成后继续。")
            else:
                self._abort_session_gate(gate)
                self._set_status("已有保存正在进行；完成后可再次保存或关闭。")
            return
        if self._session_save_dialog_open:
            self._abort_session_gate(gate)
            self._set_status("请先完成当前的保存位置选择。")
            return
        generation = self._session_generation
        default = self.project_path or default_project_path(document.media_root, document.drama)
        self._session_save_dialog_open = True
        try:
            path, _filter = QFileDialog.getSaveFileName(
                self, "保存工程", str(default), "本地切片工程 (*.localcut.json)"
            )
        finally:
            self._session_save_dialog_open = False
        if not path:
            self._abort_session_gate(gate, "已取消保存，当前工程保持不变。")
            return
        if self.document is not document or generation != self._session_generation:
            self._abort_session_gate(gate)
            self._set_status("当前工程已切换，未保存旧的选择；请重新保存。")
            return
        if not path.endswith(".localcut.json"):
            path += ".localcut.json"
        # JSON is both the immutable baseline and the source of the worker copy.
        content = self._project_content(document)
        try:
            snapshot = ProjectDocument.from_dict(json.loads(content))
        except Exception as exc:
            self._abort_session_gate(gate)
            self._error(exc)
            return
        request = _PendingSave(document, generation, content, gate)
        self._session_save = request

        def task(_progress: Callable[[str], None], cancel: Event) -> Path:
            if cancel.is_set():
                raise ExportCancelled("工程保存已取消。")
            # Once an atomic save starts, let it report its actual committed result.
            return save_project(snapshot, path)

        try:
            self._run_task(
                "正在原子保存工程…", task,
                lambda saved_path: self._on_saved(saved_path, request),
                lambda exc: self._on_project_save_failed(exc, request),
            )
        except Exception as exc:
            self._on_project_save_failed(exc, request)

    def _on_saved(self, path: Path, request: _PendingSave | None = None) -> None:
        request = request or self._session_save
        if request is None or self._session_save is not request:
            return
        self._session_save = None
        current = self.document is request.document and self._session_generation == request.generation
        if current:
            self.project_path = Path(path)
            self._baseline_document = request.document
            self._baseline_content = request.content
            self._refresh_project_session_state()
            if self._project_is_dirty():
                self._set_status("工程快照已保存；保存期间的新修改尚未保存。")
            else:
                self._set_status(f"工程已保存，并保留恢复副本：{Path(path).name}.recovery")
        else:
            self._set_status(f"旧工程快照已保存：{Path(path).name}；当前工程保持不变。")
        if request.gate is not None:
            if current and not self._project_is_dirty():
                self._continue_session_gate(request.gate, request.content)
            else:
                self._abort_session_gate(request.gate, "保存期间工程有变化，已保留当前编辑；未继续关闭或切换。")

    def _on_project_save_failed(self, exc: Exception, request: _PendingSave) -> None:
        if self._session_save is not request:
            return
        self._session_save = None
        self._abort_session_gate(request.gate)
        if isinstance(exc, ExportCancelled):
            self._set_status(str(exc))
        else:
            self._error(exc)
