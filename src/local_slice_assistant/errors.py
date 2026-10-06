"""面向用户的稳定错误类型。

界面和命令行只展示这些错误的中文说明，避免把 FFmpeg、路径或 JSON 的内部堆栈
直接抛给剪辑者。
"""

from __future__ import annotations


class LocalSliceError(Exception):
    """可安全展示给用户的错误。"""

    code = "LOCAL_SLICE_ERROR"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message}\n{self.detail}"
        return self.message


class ManifestValidationError(LocalSliceError):
    code = "MANIFEST_INVALID"


class MediaProbeError(LocalSliceError):
    code = "MEDIA_PROBE_FAILED"


class ProjectLoadError(LocalSliceError):
    code = "PROJECT_LOAD_FAILED"


class ProjectSaveError(LocalSliceError):
    code = "PROJECT_SAVE_FAILED"


class ExportError(LocalSliceError):
    code = "EXPORT_FAILED"


class ExportCancelled(ExportError):
    code = "EXPORT_CANCELLED"


class AnalysisCancelled(LocalSliceError):
    code = "ANALYSIS_CANCELLED"


class VisionError(LocalSliceError):
    """本地画面辅助的可恢复错误；基础手动剪辑继续可用。"""

    code = "VISION_FAILED"


class VisionBackendUnavailable(VisionError):
    code = "VISION_BACKEND_UNAVAILABLE"


class VisionCancelled(VisionError):
    code = "VISION_CANCELLED"
