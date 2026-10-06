"""User-supplied audio only: no speech provider or separation service calls."""
from pathlib import Path
from uuid import uuid4

from .ffmpeg import run_ffmpeg
from .narration_plan import require_current_plan
from .narration_mix import file_sha256, wav_duration, mix_narration
from .narration_pipeline import extract_original_audio, source_identity


def prepare_manual_narration(document, cut_id, audio_files, progress, cancel):
    cut = document.get_cut(cut_id)
    plan = cut.packaging.get("narration_plan")
    if not plan:
        raise ValueError("请先导入带成片时间的解说稿。")
    require_current_plan(plan, cut)
    if set(audio_files) != {cue["id"] for cue in plan["cues"]}:
        raise ValueError("请为每句解说匹配一份音频，不允许漏句或多余编号。")
    if any(cue["original_audio"] == "remove_dialogue" for cue in plan["cues"]):
        raise ValueError("本版不自动分离人声；请将解说原声策略明确设为 keep（降低原声）或 mute（静音）。")
    identity = source_identity(document, cut)
    root = Path(document.media_root) / ".local_slice_assistant" / "manual_narration" / uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    jobs = []
    for index, cue in enumerate(plan["cues"]):
        if cancel.is_set():
            raise ValueError("已取消本地音频匹配，未应用到工程。")
        source = Path(audio_files[cue["id"]]).resolve(strict=True)
        target = root / f"{index + 1:03d}.wav"
        progress(f"正在校验自带音频 {index+1}/{len(plan['cues'])}：{cue['id']}")
        run_ffmpeg(["-nostdin", "-n", "-i", str(source), "-vn", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(target)], cancel_event=cancel)
        duration = wav_duration(target)
        if duration <= 0 or duration > cue["end_us"] - cue["start_us"]:
            raise ValueError(f"{cue['id']}：音频为空或超过指定解说时间窗；未截断，请调整时间或音频。")
        jobs.append(dict(cue_id=cue["id"], state="ready", wav_path=str(target), audio_sha256=file_sha256(target)))
    original = root / "original.wav"
    extract_original_audio(document, cut, original, progress, cancel)
    result = mix_narration(dict(plan=plan, state="ready", jobs=jobs), cut, original, None, root / "mixed.wav", progress=progress, cancel=cancel)
    if source_identity(document, cut) != identity:
        raise ValueError("处理期间素材发生变化，未应用混音。")
    result["source_identity"] = identity
    return result
