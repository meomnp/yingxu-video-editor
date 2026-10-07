"""第三阶段：只分析用户点选接缝附近的本地时序画面。

本模块刻意不把模型输出写回工程。视觉模型只能在第二阶段已经给出的、
可核对的候选中做选择，或明确请求扩大人工核对范围／放弃判断；真正采用
仍由 GUI 调用现有的、可撤销的片段调整命令。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Any, Iterable

from .analysis import JunctionAnalysis, JunctionCandidate, MICROSECONDS
from .analysis_cache import AnalysisCache, project_cache_root
from .errors import (
    AnalysisCancelled,
    ManifestValidationError,
    VisionBackendUnavailable,
    VisionCancelled,
    VisionError,
)
from .ffmpeg import ffmpeg_binary
from .models import ProjectDocument, Segment
from .paths import resolve_excluded_dirs, resolve_media_root, safe_resolve_media_path
from .resources import GIB, ResourceMeter, snapshot
from .timeline import format_timecode_us
from .runtime_settings import runtime_setting


DEFAULT_WINDOW_US = 5 * MICROSECONDS
MAX_WINDOW_US = 10 * MICROSECONDS
# SmolVLM2-2.2B 的本机实测显示：一次塞入 16 张独立图片时会偏向最后几张，
# 8 张（每侧 4 张）能保留可靠的前后时序描述。16 仍是绝对上限，供以后有
# 经验证的更强后端或局部加密时使用，而不是伪造“更多帧一定更好”。
MAX_FRAME_BUDGET = 16
DEFAULT_FRAME_BUDGET = 8
SAVER_FRAME_BUDGET = 6
DEFAULT_LONG_EDGE = 512
SAVER_LONG_EDGE = 384
VISION_TIMEOUT_SECONDS = 150

_APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Yingxu"
DEFAULT_RUNTIME = Path(runtime_setting(
    "LOCAL_SLICE_VISION_RUNTIME",
    _APP_DATA / "vision" / "llama.cpp" / "llama-mtmd-cli.exe",
))
DEFAULT_MODEL_ROOT = Path(runtime_setting(
    "LOCAL_SLICE_VISION_MODEL_ROOT",
    _APP_DATA / "vision" / "SmolVLM2-2.2B-Instruct-GGUF",
))
DEFAULT_MODEL = DEFAULT_MODEL_ROOT / "SmolVLM2-2.2B-Instruct-Q4_K_M.gguf"
DEFAULT_MMPROJ = DEFAULT_MODEL_ROOT / "mmproj-SmolVLM2-2.2B-Instruct-Q8_0.gguf"


@dataclass(frozen=True, slots=True)
class VisionBackend:
    """一个固定、可核验的本地视觉后端，而非模糊的“模型名称”。"""

    executable: Path = DEFAULT_RUNTIME
    model: Path = DEFAULT_MODEL
    projector: Path = DEFAULT_MMPROJ
    model_id: str = "ggml-org/SmolVLM2-2.2B-Instruct-GGUF:Q4_K_M"
    model_revision: str = "1bc3c9f74ceafd4c8d4411cc9cf188bba3798f91"
    runtime_build: str = "llama.cpp 0.4.1-dev build 11029 (CUDA 12.4 x64)"

    @classmethod
    def default(cls) -> "VisionBackend":
        return cls()

    def identity(self) -> dict[str, str]:
        return {
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "runtime_build": self.runtime_build,
            "model_name": self.model.name,
            "projector_name": self.projector.name,
        }

    def validate_available(self) -> None:
        missing = [
            label
            for label, path in (
                ("llama.cpp 视觉运行时", self.executable),
                ("SmolVLM2 模型", self.model),
                ("SmolVLM2 视觉投影器", self.projector),
            )
            if not path.is_file()
        ]
        if missing:
            raise VisionBackendUnavailable(
                "本地画面辅助尚未准备好；基础剪辑和手工接缝调整不受影响。"
                "请运行项目根目录的“安装本地画面辅助.cmd”，或发行包 EXE 同级的 install_local_vision.cmd 后再试。",
                detail="缺少：" + "、".join(missing),
            )


@dataclass(frozen=True, slots=True)
class VisionSampling:
    window_us: int = DEFAULT_WINDOW_US
    frame_budget: int = DEFAULT_FRAME_BUDGET
    long_edge: int = DEFAULT_LONG_EDGE
    reduced_for_resources: bool = False

    def validate(self) -> None:
        if not 1 <= self.window_us <= MAX_WINDOW_US:
            raise ManifestValidationError("画面辅助窗口必须在 0–10 秒之间。")
        if not 2 <= self.frame_budget <= MAX_FRAME_BUDGET:
            raise ManifestValidationError("画面辅助帧数必须为 2–16 帧。")
        if not 64 <= self.long_edge <= DEFAULT_LONG_EDGE:
            raise ManifestValidationError("画面辅助长边必须为 64–512 像素。")

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_us": self.window_us,
            "frame_budget": self.frame_budget,
            "long_edge": self.long_edge,
            "reduced_for_resources": self.reduced_for_resources,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VisionSampling":
        return cls(
            window_us=int(raw["window_us"]),
            frame_budget=int(raw["frame_budget"]),
            long_edge=int(raw["long_edge"]),
            reduced_for_resources=bool(raw.get("reduced_for_resources", False)),
        )


@dataclass(frozen=True, slots=True)
class VisionFrame:
    order: int
    side: str
    source_file: str
    source_us: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "side": self.side,
            "source_file": self.source_file,
            "source_us": self.source_us,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VisionFrame":
        return cls(
            order=int(raw["order"]),
            side=str(raw["side"]),
            source_file=str(raw["source_file"]),
            source_us=int(raw["source_us"]),
        )


@dataclass(frozen=True, slots=True)
class VisionAnalysis:
    revision: int
    cut_id: str
    junction_index: int
    left_segment_id: str
    right_segment_id: str
    sampling: VisionSampling
    model: dict[str, str]
    frames: tuple[VisionFrame, ...]
    visible_content: str
    possible_missing_reaction_or_action: str
    recommendation: str
    candidate_index: int | None
    reason: str
    undecidable: str
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "cut_id": self.cut_id,
            "junction_index": self.junction_index,
            "left_segment_id": self.left_segment_id,
            "right_segment_id": self.right_segment_id,
            "sampling": self.sampling.to_dict(),
            "model": self.model,
            "frames": [frame.to_dict() for frame in self.frames],
            "visible_content": self.visible_content,
            "possible_missing_reaction_or_action": self.possible_missing_reaction_or_action,
            "recommendation": self.recommendation,
            "candidate_index": self.candidate_index,
            "reason": self.reason,
            "undecidable": self.undecidable,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, from_cache: bool = False) -> "VisionAnalysis":
        candidate_index = raw.get("candidate_index")
        return cls(
            revision=int(raw["revision"]),
            cut_id=str(raw["cut_id"]),
            junction_index=int(raw["junction_index"]),
            left_segment_id=str(raw["left_segment_id"]),
            right_segment_id=str(raw["right_segment_id"]),
            sampling=VisionSampling.from_dict(dict(raw["sampling"])),
            model={str(key): str(value) for key, value in dict(raw["model"]).items()},
            frames=tuple(VisionFrame.from_dict(item) for item in raw.get("frames", [])),
            visible_content=str(raw["visible_content"]),
            possible_missing_reaction_or_action=str(
                raw["possible_missing_reaction_or_action"]
            ),
            recommendation=str(raw["recommendation"]),
            candidate_index=(None if candidate_index is None else int(candidate_index)),
            reason=str(raw["reason"]),
            undecidable=str(raw["undecidable"]),
            from_cache=from_cache,
        )


def sampling_for_mode(mode: str) -> VisionSampling:
    if mode == "saver":
        return VisionSampling(
            frame_budget=SAVER_FRAME_BUDGET,
            long_edge=SAVER_LONG_EDGE,
        )
    return VisionSampling()


def _source_path(document: ProjectDocument, source_file: str) -> Path:
    root = resolve_media_root(document.media_root)
    _relative, path = safe_resolve_media_path(
        root, source_file, exclusions=resolve_excluded_dirs(root)
    )
    return path


def _sample_points(start_us: int, end_us: int, count: int) -> list[int]:
    """在半开区间中均匀取点，避免把右侧边界误算进前段。"""

    if end_us <= start_us:
        return [start_us]
    amount = max(1, count)
    span = end_us - start_us
    return [
        min(end_us - 1, start_us + (span * (index * 2 + 1)) // (amount * 2))
        for index in range(amount)
    ]


def plan_junction_frames(
    left: Segment,
    right: Segment,
    *,
    sampling: VisionSampling,
) -> tuple[VisionFrame, ...]:
    """生成稳定的左右帧序，不依赖或泄漏代理帧号。"""

    sampling.validate()
    left_count = max(1, sampling.frame_budget // 2)
    right_count = max(1, sampling.frame_budget - left_count)
    left_start = max(left.in_us, left.out_us - sampling.window_us)
    right_end = min(right.out_us, right.in_us + sampling.window_us)
    planned: list[VisionFrame] = []
    for source_us in _sample_points(left_start, left.out_us, left_count):
        planned.append(
            VisionFrame(
                order=len(planned) + 1,
                side="left",
                source_file=left.source_file,
                source_us=source_us,
            )
        )
    for source_us in _sample_points(right.in_us, right_end, right_count):
        planned.append(
            VisionFrame(
                order=len(planned) + 1,
                side="right",
                source_file=right.source_file,
                source_us=source_us,
            )
        )
    return tuple(planned[: sampling.frame_budget])


def _seconds(value_us: int) -> str:
    return f"{value_us / MICROSECONDS:.6f}"


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_child(
    command: list[str],
    *,
    timeout_seconds: float,
    cancel_event: Event | None,
    error_message: str,
    resource_meter: ResourceMeter | None = None,
    visual: bool = False,
) -> bytes:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise VisionError(error_message, detail=str(exc)) from exc
    if resource_meter and visual:
        resource_meter.track_visual_process(process.pid)
    started = time.monotonic()
    try:
        while True:
            if resource_meter:
                resource_meter.observe(process.pid)
            if cancel_event and cancel_event.is_set():
                _stop_process(process)
                if resource_meter and visual:
                    resource_meter.mark_visual_process_exited(process.pid)
                raise VisionCancelled("本地画面辅助已取消，模型进程已停止。")
            if time.monotonic() - started > timeout_seconds:
                _stop_process(process)
                if resource_meter and visual:
                    resource_meter.mark_visual_process_exited(process.pid)
                raise VisionError("本地画面辅助超时，模型进程已停止。")
            try:
                stdout, stderr = process.communicate(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            _stop_process(process)
    if resource_meter:
        resource_meter.observe(process.pid)
        if visual:
            resource_meter.mark_visual_process_exited(process.pid)
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace")[-2000:]
        raise VisionError(error_message, detail=detail)
    return stdout


def _extract_frames(
    document: ProjectDocument,
    frames: Iterable[VisionFrame],
    *,
    sampling: VisionSampling,
    work_dir: Path,
    cancel_event: Event | None,
    resource_meter: ResourceMeter | None,
) -> list[Path]:
    paths: list[Path] = []
    for frame in frames:
        if cancel_event and cancel_event.is_set():
            raise VisionCancelled("本地画面辅助已取消，未继续读取画面。")
        output = work_dir / f"{frame.order:02d}-{frame.side}.jpg"
        _run_child(
            [
                ffmpeg_binary(),
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-i",
                os.fspath(_source_path(document, frame.source_file)),
                "-ss",
                _seconds(frame.source_us),
                "-frames:v",
                "1",
                "-vf",
                (
                    f"scale={sampling.long_edge}:{sampling.long_edge}:"
                    "force_original_aspect_ratio=decrease:force_divisible_by=2"
                ),
                "-q:v",
                "3",
                "-y",
                os.fspath(output),
            ],
            timeout_seconds=45,
            cancel_event=cancel_event,
            error_message="无法读取所选接缝附近的原始画面。",
            resource_meter=resource_meter,
        )
        if not output.is_file() or output.stat().st_size <= 0:
            raise VisionError("局部画面抽取没有生成可用帧。")
        paths.append(output)
    return paths


def _candidate_payload(candidates: Iterable[JunctionCandidate]) -> list[dict[str, Any]]:
    return [
        {
            "side": candidate.side,
            "source_file": candidate.source_file,
            "current_us": candidate.current_us,
            "proposed_us": candidate.proposed_us,
            "kind": candidate.kind,
            "reason": candidate.reason,
            "evidence": candidate.evidence,
        }
        for candidate in candidates
    ]


def _compact_cues(analysis: JunctionAnalysis) -> list[dict[str, Any]]:
    return [
        {
            "side": side,
            "source_file": cue.source_file,
            "start_us": cue.start_us,
            "end_us": cue.end_us,
            "text": cue.text,
        }
        for side, cues in (("left", analysis.left_cues), ("right", analysis.right_cues))
        for cue in cues
    ]


def _system_prompt(
    frames: Iterable[VisionFrame],
    analysis: JunctionAnalysis,
    candidates: list[JunctionCandidate],
) -> str:
    frame_lines = "\n".join(
        f"F{frame.order:02d} {frame.side} {frame.source_file} "
        f"{format_timecode_us(frame.source_us)}"
        for frame in frames
    )
    subtitles = "\n".join(
        f"{item['side']} {format_timecode_us(int(item['start_us']))}–"
        f"{format_timecode_us(int(item['end_us']))}: {item['text']}"
        for item in _compact_cues(analysis)
    ) or "(no nearby subtitle evidence)"
    candidate_lines = "\n".join(
        f"C{index}: {candidate.side}, {format_timecode_us(candidate.current_us)} -> "
        f"{format_timecode_us(candidate.proposed_us)}, {candidate.kind}, "
        f"{candidate.reason}; evidence: {candidate.evidence}"
        for index, candidate in enumerate(candidates)
    ) or "(no deterministic candidate is available)"
    return f"""You are a conservative local video-junction reviewer. The image frames are in strict temporal order. Frames marked left occur before the current edit boundary; frames marked right occur after it. Do not invent events that are not visibly supported. A sparse image sequence cannot prove a full action.

FRAME ORDER AND SOURCE TIMES:
{frame_lines}

NEARBY SUBTITLES (may be absent or imperfect evidence):
{subtitles}

EDIT PURPOSE: preserve a coherent short-drama beat while avoiding an unsupported cut.

THE ONLY EXISTING DETERMINISTIC CANDIDATES (never invent another one or a timestamp):
{candidate_lines}

The editor will make the actual cut decision. Your immediate task is only to describe visible before/after evidence and state its limits.
"""


def _visual_prompt(frames: tuple[VisionFrame, ...]) -> str:
    left_count = sum(frame.side == "left" for frame in frames)
    right_count = len(frames) - left_count
    return (
        f"The first {left_count} images are earlier and the last {right_count} images "
        "are later. In two short sentences, describe what is visibly different, then say "
        "whether the pictures alone can prove a complete action or reaction."
    )


def _normalize_observation(raw: bytes) -> tuple[str, str, str, int | None, str, str]:
    """把真实模型的自然语言画面说明转成保守的应用结果。

    首轮后端在强制 JSON、候选表与多帧同时出现时会复述提示词；本机验证后改为
    把台词／目的／候选放进 system context，让模型只回答画面本身。它没有经过
    可靠验证来直接选择帧级候选，因此任何输出都明确回到人工核对，而不是伪造
    一个模型选择的候选。
    """

    observation = raw.decode("utf-8", errors="replace").strip()
    if not observation:
        raise VisionError("本地视觉模型没有返回画面说明，未生成建议。")
    normalized = observation.casefold()
    uncertainty_markers = (
        "cannot",
        "can't",
        "unclear",
        "not enough",
        "unable",
        "无法",
        "不能",
        "不确定",
    )
    uncertain = any(marker in normalized for marker in uncertainty_markers)
    return (
        observation,
        "模型没有可靠主张具体缺失的反应或动作；请结合原片预览核对。",
        "expand_manual_review" if uncertain else "undecidable",
        None,
        "模型只提供局部时序画面证据，当前后端不直接替用户选择切点。",
        observation
        if uncertain
        else "这组局部画面不足以让当前后端安全选择一个精确切点。",
    )


def _gpu_free_bytes() -> int | None:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode:
        return None
    values: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            values.append(int(float(line.strip())) * 1024 * 1024)
        except ValueError:
            continue
    return max(values) if values else None


def _fit_resources(sampling: VisionSampling) -> VisionSampling:
    """只降低一次局部采样；不足时给出可理解的手工回退原因。"""

    current = snapshot()
    needs_reduction = bool(
        current.available_memory_bytes is not None
        and current.available_memory_bytes < 8 * GIB
    )
    free_gpu = _gpu_free_bytes()
    if free_gpu is None:
        raise VisionBackendUnavailable(
            "未检测到可供本地画面辅助使用的 NVIDIA CUDA 环境；请使用手工接缝核对。"
        )
    if free_gpu < 3 * GIB:
        needs_reduction = True
    effective = sampling
    if needs_reduction and not sampling.reduced_for_resources:
        effective = replace(
            sampling,
            frame_budget=min(sampling.frame_budget, SAVER_FRAME_BUDGET),
            long_edge=min(sampling.long_edge, SAVER_LONG_EDGE),
            reduced_for_resources=True,
        )
    if (
        current.available_memory_bytes is not None
        and current.available_memory_bytes < 4 * GIB
    ):
        raise VisionBackendUnavailable(
            "系统可用内存不足 4 GiB；已停止启动视觉模型，请先释放内存后再试。"
        )
    if free_gpu < 3 * GIB:
        raise VisionBackendUnavailable(
            "可用显存不足约 3 GiB；已停止启动视觉模型，请释放显存或改用手工接缝核对。"
        )
    effective.validate()
    return effective


def analyze_visual_junction(
    document: ProjectDocument,
    *,
    cut_id: str,
    junction_index: int,
    base_analysis: JunctionAnalysis,
    sampling: VisionSampling,
    backend: VisionBackend | None = None,
    cache: AnalysisCache | None = None,
    cancel_event: Event | None = None,
    resource_meter: ResourceMeter | None = None,
) -> VisionAnalysis:
    """运行一个 llama-mtmd-cli 子进程，并在返回前释放它的模型资源。"""

    if cancel_event and cancel_event.is_set():
        raise VisionCancelled("本地画面辅助已取消。")
    selected_backend = backend or VisionBackend.default()
    selected_backend.validate_available()
    cut = document.get_cut(cut_id)
    if not 0 <= junction_index < len(cut.segments) - 1:
        raise ManifestValidationError("请选择有效的相邻片段接缝。")
    left, right = cut.segments[junction_index], cut.segments[junction_index + 1]
    if (
        base_analysis.revision != document.revision
        or base_analysis.cut_id != cut_id
        or base_analysis.junction_index != junction_index
        or base_analysis.left_segment_id != left.id
        or base_analysis.right_segment_id != right.id
    ):
        raise ManifestValidationError("基础接缝证据已过期；请先重新运行基础分析。")
    effective_sampling = _fit_resources(sampling)
    frames = plan_junction_frames(left, right, sampling=effective_sampling)
    candidates = list(base_analysis.candidates)
    cache_payload = {
        "version": 1,
        "revision": document.revision,
        "cut_id": cut_id,
        "junction_index": junction_index,
        "left": [left.id, left.source_file, left.in_us, left.out_us],
        "right": [right.id, right.source_file, right.in_us, right.out_us],
        "source_hashes": {
            left.source_file: document.source_for(left.source_file).quick_hash,
            right.source_file: document.source_for(right.source_file).quick_hash,
        },
        "sampling": effective_sampling.to_dict(),
        "model": selected_backend.identity(),
        "frames": [frame.to_dict() for frame in frames],
        "candidates": _candidate_payload(candidates),
        "cues": _compact_cues(base_analysis),
    }
    key = AnalysisCache.key_for(cache_payload)
    if cache:
        cached = cache.get(key)
        if cached:
            return VisionAnalysis.from_dict(cached, from_cache=True)

    cache_root = project_cache_root(document.media_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="vision-", dir=cache_root))
    try:
        image_paths = _extract_frames(
            document,
            frames,
            sampling=effective_sampling,
            work_dir=work_dir,
            cancel_event=cancel_event,
            resource_meter=resource_meter,
        )
        command = [
            os.fspath(selected_backend.executable),
            "-m",
            os.fspath(selected_backend.model),
            "--mmproj",
            os.fspath(selected_backend.projector),
            "--n-gpu-layers",
            "999",
            "--ctx-size",
            "4096",
            "--threads",
            "6",
            "--temp",
            "0.1",
            "--predict",
            "120",
            "--system-prompt",
            _system_prompt(frames, base_analysis, candidates),
        ]
        for image_path in image_paths:
            command.extend(["--image", os.fspath(image_path)])
        command.extend(["-p", _visual_prompt(frames)])
        raw = _run_child(
            command,
            timeout_seconds=VISION_TIMEOUT_SECONDS,
            cancel_event=cancel_event,
            error_message="本地视觉模型无法完成当前接缝分析。",
            resource_meter=resource_meter,
            visual=True,
        )
    except AnalysisCancelled as exc:
        raise VisionCancelled("本地画面辅助已取消。") from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    visible, missing, recommendation, candidate_index, reason, undecidable = _normalize_observation(raw)
    result = VisionAnalysis(
        revision=document.revision,
        cut_id=cut_id,
        junction_index=junction_index,
        left_segment_id=left.id,
        right_segment_id=right.id,
        sampling=effective_sampling,
        model=selected_backend.identity(),
        frames=frames,
        visible_content=visible,
        possible_missing_reaction_or_action=missing,
        recommendation=recommendation,
        candidate_index=candidate_index,
        reason=reason,
        undecidable=undecidable,
    )
    if cache:
        cache.put(key, result.to_dict())
    return result
