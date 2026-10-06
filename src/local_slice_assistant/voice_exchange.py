"""单句文件协议 v1：只接收精确绑定、完整且未超时的 PCM 音频。"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4
import wave
import zipfile

from .packaging import normalize_packaging


def segment_signature(segment):
    return [segment.id, segment.source_file, segment.in_us, segment.out_us, segment.speed_percent]


def prepare_request(segment, text):
    text = text.strip()
    if not text or len(text) > 300:
        raise ValueError("请输入 1～300 字解说词，首次建议只试一句。")
    if not 1 <= segment.duration_us <= 900_000_000:
        raise ValueError("单句承载片段须在 15 分钟以内。")
    request = dict(schema_version=1, package_id=uuid4().hex, segment_id=segment.id,
                   text=text, max_duration_us=segment.duration_us)
    return dict(request=request, segment_signature=segment_signature(segment))


def read_result(path, cut):
    def request_for_result(result):
        packaging = normalize_packaging(deepcopy(cut.packaging))
        job = packaging.get("voice_requests", {}).get(result.get("segment_id"))
        if not isinstance(job, dict):
            raise ValueError("当前切片没有对应的配音任务，请先导出任务。")
        request = job["request"]
        segment = next((s for s in cut.segments if s.id == request["segment_id"]), None)
        if segment is None or segment_signature(segment) != job["segment_signature"]:
            raise ValueError("承载片段已经剪切或变速，请重新导出配音任务。")
        if any(item.get("voice_package_id") == request["package_id"] for item in packaging["audio_items"]):
            raise ValueError("这份配音已经导入，不会重复叠加。")
        return request
    return _read_package(path, request_for_result)


def read_voice_package(path, request):
    """Batch consumers provide the exact persisted request, not a guessed cue."""
    return _read_package(path, lambda _result: request)


def _read_package(path, request_for_result):
    """Read bounded archive without extracting any archive-provided paths."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [entry.filename for entry in infos]
        if len(names) != 2 or len(set(names)) != 2 or "result.json" not in names:
            raise ValueError("配音包须且只能包含 result.json 和一份 WAV。")
        if archive.getinfo("result.json").file_size > 256_000:
            raise ValueError("配音回执过大。")
        result = json.loads(archive.read("result.json").decode("utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError("配音回执格式错误。")
        request = request_for_result(result)
        if any(type(result.get(key)) is not type(value) or result.get(key) != value for key, value in request.items()):
            raise ValueError("配音包编号、台词或时长预算不匹配；请导入本次任务的结果。")
        audio_name = f"audio/{request['segment_id']}.wav"
        if result.get("audio_file") != audio_name or set(names) != {"result.json", audio_name}:
            raise ValueError("配音包音频路径不匹配。")
        if archive.getinfo(audio_name).file_size > 128 * 1024 * 1024:
            raise ValueError("单句音频超过 128 MiB 限制。")
        data = archive.read(audio_name)
    if sha256(data).hexdigest() != result.get("audio_sha256"):
        raise ValueError("音频校验失败，文件可能已损坏。")
    with wave.open(BytesIO(data), "rb") as reader:
        channels, width, rate, frames = reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getnframes()
        if reader.getcomptype() != "NONE" or not (1 <= channels <= 8 and width in (1, 2, 3, 4) and 8000 <= rate <= 192000 and frames > 0):
            raise ValueError("只接受有效 PCM WAV。")
        payload = reader.readframes(frames)
        if len(payload) != frames * channels * width:
            raise ValueError("WAV 音频数据不完整。")
    measured = dict(channels=channels, sample_width_bytes=width, sample_rate_hz=rate,
                    frames=frames, duration_us=(frames * 1_000_000 + rate // 2) // rate)
    if any(type(result.get(k)) is not int or result[k] != v for k, v in measured.items()):
        raise ValueError("配音回执与实际音频时长或格式不一致。")
    if frames * 1_000_000 > request["max_duration_us"] * rate:
        raise ValueError("配音超过画面时长，未导入，也不会截断。")
    return result, data


def attach_result(cut, result, audio_path, mute_original=True):
    packaging = normalize_packaging(deepcopy(cut.packaging))
    packaging["audio_items"].append(dict(
        id=uuid4().hex, kind="voiceover", file_path=str(Path(audio_path).resolve()),
        anchor_segment_id=result["segment_id"], source_offset_us=0, source_in_us=0,
        duration_us=result["duration_us"], enabled=True, mute_original=mute_original,
        volume=1.0, script=result["text"], voice_package_id=result["package_id"],
    ))
    return packaging
