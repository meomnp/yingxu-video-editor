"""Deterministic interval replacement: original outside narration, stems inside."""
from hashlib import sha256
import json
from pathlib import Path
import wave
from tempfile import TemporaryDirectory

from .ffmpeg import run_ffmpeg
from .narration_plan import require_current_plan
from .timeline import total_duration_us
from .paths import file_identity, safe_resolve_media_path


def file_sha256(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def wav_duration(path):
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes() * 1_000_000 / reader.getframerate()


def plan_digest(plan):
    return sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def apply_narration_render(filters, inputs, current_audio, cut, document=None):
    plan = cut.packaging.get("narration_plan")
    if not plan:
        return current_audio
    require_current_plan(plan, cut)
    render = cut.packaging.get("narration_render")
    if not render or render.get("plan_digest") != plan_digest(plan):
        raise ValueError("解说计划尚未生成完整混音或已修改，请先完成解说制作。")
    if document is not None:
        sources = {name: list(file_identity(safe_resolve_media_path(document.media_root, name)[1]))
                   for name in sorted({segment.source_file for segment in cut.segments})}
        if render.get("source_identity") != sources:
            raise ValueError("原素材已变化或混音缺少素材校验记录，请重新制作解说。")
    path = Path(render["path"])
    if file_sha256(path) != render["sha256"] or abs(wav_duration(path) - total_duration_us(cut)) > 1000:
        raise ValueError("解说混音文件丢失、损坏或时长不匹配，请重新制作。")
    index = len([value for value in inputs if value == "-i"])
    inputs.extend(["-i", str(path)])
    filters.append(f"{current_audio}anullsink")
    filters.append(f"[{index}:a]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[narration_base]")
    return "[narration_base]"


def _assemble_voice_track(batch, folder, duration, progress, cancel):
    """Stream sparse speech onto one PCM track, keeping input count bounded."""
    target = Path(folder) / "voices.wav"
    jobs = {job['cue_id']: job for job in batch['jobs']}
    cursor = 0
    total = round(duration * 48000 / 1000000)
    with wave.open(str(target), 'wb') as writer:
        writer.setparams((2, 3, 48000, 0, 'NONE', 'NONE'))
        def silence(frames):
            while frames:
                if cancel and cancel.is_set():
                    raise ValueError('已停止配音轨组装。')
                count = min(frames, 48000)
                writer.writeframesraw(b'\x00' * count * 6)
                frames -= count
        for index, cue in enumerate(batch['plan']['cues']):
            job = jobs[cue['id']]
            path = Path(job['wav_path'])
            if file_sha256(path) != job['audio_sha256']:
                raise ValueError('配音文件发生变化，未使用。')
            if wav_duration(path) > cue['end_us'] - cue['start_us']:
                raise ValueError('实际配音超出解说时间窗，未截断。')
            if progress:
                progress(f"对齐配音 {index+1}/{len(batch['jobs'])}")
            converted = Path(folder) / f'{index}.wav'
            run_ffmpeg(['-nostdin', '-n', '-i', str(path), '-ar', '48000', '-ac', '2',
                        '-c:a', 'pcm_s24le', str(converted)], cancel_event=cancel)
            start = (cue['start_us'] * 48000 + 500000) // 1000000
            end = (cue['end_us'] * 48000 + 500000) // 1000000
            if start < cursor:
                raise ValueError('配音采样位置重叠，未覆盖上一句。')
            silence(start - cursor)
            with wave.open(str(converted), 'rb') as reader:
                frames = reader.getnframes()
                if start + frames > end:
                    raise ValueError('重采样配音超过时间窗，未截断。')
                while block := reader.readframes(48000):
                    if cancel and cancel.is_set():
                        raise ValueError('已停止配音轨组装。')
                    writer.writeframesraw(block)
                cursor = start + frames
        silence(total - cursor)
    return target


def mix_narration(batch, cut, original_wav, background_wav, output, *, progress=None, cancel=None):
    require_current_plan(batch['plan'], cut)
    if batch['state'] != 'ready' or any(job['state'] != 'ready' for job in batch['jobs']):
        raise ValueError('批次还有未完成配音，不能把缺句混成完整成片。')
    if len(batch['jobs']) > 16:
        with TemporaryDirectory(prefix='localcut-voice-') as folder:
            track = _assemble_voice_track(batch, folder, total_duration_us(cut), progress, cancel)
            return _mix_narration(batch, cut, original_wav, background_wav, output,
                                  progress=progress, cancel=cancel, voice_track=track)
    return _mix_narration(batch, cut, original_wav, background_wav, output, progress=progress, cancel=cancel)


def _mix_narration(batch, cut, original_wav, background_wav, output, *, progress=None, cancel=None, voice_track=None):
    require_current_plan(batch["plan"], cut)
    if batch["state"] != "ready" or any(job["state"] != "ready" for job in batch["jobs"]):
        raise ValueError("批次还有未完成配音，不能把缺句混成完整成片。")
    duration = total_duration_us(cut)
    if abs(wav_duration(original_wav) - duration) > 1000:
        raise ValueError("原声轨时长与成片不匹配。")
    cues = batch["plan"]["cues"]
    need_background = any(cue["original_audio"] == "remove_dialogue" for cue in cues)
    if need_background and (not background_wav or abs(wav_duration(background_wav) - duration) > 1000):
        raise ValueError("缺少与成片对齐的真实分离背景轨，不能用静音代替。")
    output = Path(output)
    if output.exists():
        raise ValueError("混音输出已存在，不覆盖旧版本。")
    inputs = ["-i", str(original_wav)]
    if need_background:
        inputs += ["-i", str(background_wav)]
    jobs = {job["cue_id"]: job for job in batch["jobs"]}
    filters, pieces, voices = [], [], []

    def piece(start, end, input_index, gain=1):
        if end <= start:
            return
        name = f"bed{len(pieces)}"
        filters.append(f"[{input_index}:a]atrim=start={start/1e6:.6f}:end={end/1e6:.6f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,volume={gain:.9f}[{name}]")
        pieces.append(f"[{name}]")

    cursor = 0
    for cue in cues:
        piece(cursor, cue["start_us"], 0)
        mode = cue["original_audio"]
        piece(cue["start_us"], cue["end_us"], 1 if mode == "remove_dialogue" else 0,
              0 if mode == "mute" else 10 ** (cue["background_gain_db"] / 20))
        cursor = cue["end_us"]
        job = jobs[cue["id"]]
        path = Path(job["wav_path"])
        if file_sha256(path) != job["audio_sha256"]:
            raise ValueError("已生成的配音文件发生变化，请重新校验原包。")
        actual = wav_duration(path)
        if actual > cue["end_us"] - cue["start_us"]:
            raise ValueError("实际配音超出解说时间窗，未截断。")
        if voice_track:
            continue
        index = len(inputs) // 2
        inputs += ["-i", str(path)]
        label = f"voice{len(voices)}"
        delay = (cue["start_us"] * 48000 + 500000) // 1000000
        filters.append(f"[{index}:a]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,adelay={delay}S:all=1[{label}]")
        voices.append(f"[{label}]")
    piece(cursor, duration, 0)
    if voice_track:
        index = len(inputs) // 2
        inputs += ['-i', str(voice_track)]
        filters.append(f'[{index}:a]asetpts=PTS-STARTPTS[voice_track]')
        voices.append('[voice_track]')
    filters.append("".join(pieces) + f"concat=n={len(pieces)}:v=0:a=1[bed]")
    filters.append("[bed]" + "".join(voices) + f"amix=inputs={len(voices)+1}:duration=first:normalize=0:dropout_transition=0,alimiter=limit=0.95:level=0:latency=1[mix]")
    run_ffmpeg(["-nostdin", "-n", *inputs, "-filter_complex", ";".join(filters), "-map", "[mix]", "-c:a", "pcm_s24le", str(output)],
               cancel_event=cancel, progress=progress, expected_duration_us=duration)
    if abs(wav_duration(output) - duration) > 1000:
        raise ValueError("混音时长校验失败，未标记完成。")
    return dict(path=str(output.resolve()), sha256=file_sha256(output), duration_us=duration,
                timeline_fingerprint=batch["plan"]["timeline_fingerprint"], plan_digest=plan_digest(batch["plan"]))
