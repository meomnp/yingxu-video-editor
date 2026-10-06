"""Serial extraction → real stem separation → speech batch → deterministic mix."""
from copy import deepcopy
import json
import os
from pathlib import Path
from uuid import uuid4

from .batch_lock import batch_execution_lock
from .exporter import _filter_graph
from .ffmpeg import run_ffmpeg
from .narration_batch import run_batch, save_batch
from .narration_mix import mix_narration, file_sha256, wav_duration
from .narration_plan import require_current_plan
from .paths import file_identity, safe_resolve_media_path
from .separation_bridge import separate_audio, validate_separation
from .timeline import total_duration_us


def source_identity(document, cut):
    identities = {}
    for name in sorted({s.source_file for s in cut.segments}):
        current = list(file_identity(safe_resolve_media_path(document.media_root, name)[1]))
        saved = document.sources[name]
        if current[0] != saved.size or current[2] != saved.quick_hash:
            raise ValueError(f"工程原视频已变化，未按旧剪辑继续解说制作：{name}")
        identities[name] = current
    return identities


def extract_original_audio(document, cut, output, progress, cancel):
    clean = deepcopy(cut)
    # Preserve all title-card timing, but do not include old voiceovers or narration.
    clean.packaging = {"title_cards": deepcopy(cut.packaging.get("title_cards", []))}
    inputs, graph = _filter_graph(document, clean, width=320, height=180,
                                 fps_num=25, fps_den=1, include_packaging=True)
    run_ffmpeg(["-nostdin", "-n", *inputs, "-filter_complex", graph,
                "-map", "[aout]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(output),
                "-map", "[vout]", "-f", "null", os.devnull], progress=progress, cancel_event=cancel,
               expected_duration_us=total_duration_us(cut))
    if abs(wav_duration(output) - total_duration_us(cut)) > 1000:
        raise ValueError("提取的成片原声时长不匹配，未进行分离。")


def load_batch(path, cut, plan):
    path = Path(path).resolve()
    batch = json.loads(path.read_text(encoding="utf-8"))
    if Path(batch["directory"]).resolve() != path.parent or batch["plan"] != plan:
        raise ValueError("批次记录不对应当前解说计划，未继续。")
    require_current_plan(plan, cut)
    return batch


def produce_narration(document, cut_id, batch, progress, cancel, *, separator=separate_audio, batch_runner=run_batch):
    # Shared by GUI and CLI: never submit the same batch from two runners.
    with batch_execution_lock(batch['directory'], progress):
        # A caller may have loaded the batch before another runner finished.
        # Reload under the lock so stale "pending" states cannot resubmit jobs.
        fresh = load_batch(Path(batch['directory']) / 'batch.json',
                           document.get_cut(cut_id), batch['plan'])
        batch.clear()
        batch.update(fresh)
        return _produce_narration(document, cut_id, batch, progress, cancel,
                                  separator=separator, batch_runner=batch_runner)


def _produce_narration(document, cut_id, batch, progress, cancel, *, separator, batch_runner):
    cut = document.get_cut(cut_id)
    require_current_plan(batch["plan"], cut)
    root = Path(batch["directory"])
    try:
        if cancel.is_set():
            raise ValueError("已停止，未提取原声或提交配音。")
        sources = source_identity(document, cut)
        if batch.get("source_identity", sources) != sources:
            raise ValueError("原视频已变化，请重新建立解说制作批次。")
        batch["source_identity"] = sources

        def check_sources():
            if source_identity(document, cut) != sources:
                raise ValueError("制作期间原视频已变化，未继续使用当前结果；请重新核对素材。")

        original = root / "original.wav"
        if not original.exists() or batch.get("original_sha256") != file_sha256(original):
            if original.exists():
                raise ValueError("成片原声缓存损坏或不完整，未使用。请建立新批次。")
            batch["phase"] = "提取成片原声"
            save_batch(batch)
            progress(batch["phase"])
            temporary = root / (uuid4().hex + ".partial.wav")
            extract_original_audio(document, cut, temporary, progress, cancel)
            check_sources()
            os.replace(temporary, original)
            batch["original_sha256"] = file_sha256(original)
            save_batch(batch)
        if cancel.is_set():
            raise ValueError("已停止，原声缓存保留，可继续。")
        background = None
        check_sources()
        if any(cue["original_audio"] == "remove_dialogue" for cue in batch["plan"]["cues"]):
            batch["phase"] = "分离原人声与背景"
            progress(batch["phase"])
            save_batch(batch)
            if batch.get("separation_dir"):
                separated = validate_separation(original, batch["separation_dir"])
            else:
                directory = root / ("separation_" + uuid4().hex)
                separated = separator(original, directory, progress, cancel)
                batch["separation_dir"] = str(directory)
                save_batch(batch)
            background = separated["background_path"]
        if cancel.is_set():
            raise ValueError("已停止，分离结果保留，可继续。")
        check_sources()
        batch["phase"] = "逐句本地配音"
        save_batch(batch)
        batch_runner(batch, cut, progress, cancel)
        if batch["state"] != "ready" or cancel.is_set():
            return batch
        check_sources()
        batch["phase"] = "对齐与混音"
        progress(batch["phase"])
        save_batch(batch)
        output = root / ("mixed_" + uuid4().hex + ".wav")
        render = mix_narration(batch, cut, original, background, output, progress=progress, cancel=cancel)
        check_sources()
        render["source_identity"] = sources
        batch.update(render=render, phase="制作完成", state="ready")
        batch.pop("error", None)
        save_batch(batch)
        return batch
    except Exception as exc:
        batch["state"] = "stopped" if cancel.is_set() else "incomplete"
        batch["error"] = str(exc)
        save_batch(batch)
        raise
