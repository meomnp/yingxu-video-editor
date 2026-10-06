"""Timed narration is output-time data, explicitly bound to a render timeline."""
from hashlib import sha256
import json
from pathlib import Path

from .timeline import build_render_timeline, total_duration_us
from .transcripts import load_srt, load_transcriber_markdown


def timeline_fingerprint(cut):
    rows = []
    for p in build_render_timeline(cut):
        segment = p.segment
        rows.append(dict(kind=p.kind, start=p.output_in_us, end=p.output_out_us,
                         source=([segment.id, segment.source_file, segment.in_us, segment.out_us, segment.speed_percent, segment.original_audio]
                                 if segment else p.title_card)))
    return sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def import_narration(raw, cut, *, allow_bind_current=False):
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("解说脚本须使用 schema_version: 1。")
    if raw.get("time_basis") != "output":
        raise ValueError("解说时间必须明确为 output（成片时间），不能用素材时间直接安排解说。")
    fingerprint = timeline_fingerprint(cut)
    declared = raw.get("timeline_fingerprint")
    if declared != fingerprint and not (declared is None and allow_bind_current):
        raise ValueError("解说稿不对应当前剪辑时间轴，请按当前画面重新导出设计任务。")
    rows = raw.get("cues")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise ValueError("解说脚本须包含 1～500 句。")
    normalized = []
    ids = set()
    filenames = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("每句解说须是对象。")
        cue_id, text = row.get("id"), row.get("text")
        if not isinstance(cue_id, str) or not cue_id or len(cue_id) > 80 or cue_id in ids:
            raise ValueError("解说编号须唯一且不超过80字。")
        ids.add(cue_id)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300:
            raise ValueError(f"{cue_id}：解说词须为1～300字，请按句拆开。")
        start, end = row.get("start_ms"), row.get("end_ms")
        if type(start) is not int or type(end) is not int or not 0 <= start < end or end * 1000 > total_duration_us(cut):
            raise ValueError(f"{cue_id}：时间窗无效或超出成片。")
        if end - start > 900000:
            raise ValueError(f"{cue_id}：单句时间窗超过15分钟。")
        mode = row.get("original_audio", "remove_dialogue")
        gain = row.get("background_gain_db", -12)
        if mode not in ("remove_dialogue", "keep", "mute"):
            raise ValueError(f"{cue_id}：原声策略须为 remove_dialogue、keep 或 mute。")
        if type(gain) not in (int, float) or not -60 <= gain <= 0:
            raise ValueError(f"{cue_id}：背景音量须在 -60 至 0 dB。")
        spans = []
        for placement in build_render_timeline(cut):
            left, right = max(start * 1000, placement.output_in_us), min(end * 1000, placement.output_out_us)
            if right <= left:
                continue
            if placement.segment and placement.segment.original_audio == "mute" and mode != "mute":
                raise ValueError(f"{cue_id}：解说要求保留背景或原声，但覆盖到的片段已静音。请将片段 original_audio 改为 keep，或明确将本句原声策略改为 mute。")
            spans.append(dict(segment_id=placement.segment.id if placement.segment else None,
                              kind=placement.kind, output_in_us=left, output_out_us=right))
        cue = dict(id=cue_id, text=text.strip(), start_us=start * 1000, end_us=end * 1000,
                   original_audio=mode, background_gain_db=gain, spans=spans)
        if 'audio_filename' in row:
            from .narration_files import validate_audio_filename
            filename = validate_audio_filename(row['audio_filename'])
            name = Path(filename).stem.casefold()
            if name in filenames:
                raise ValueError(f'{cue_id}：同一切片内的解说音频文件名不能重复。')
            filenames.add(name)
            cue['audio_filename'] = filename
        normalized.append(cue)
    normalized.sort(key=lambda row: row["start_us"])
    if any(left["end_us"] > right["start_us"] for left, right in zip(normalized, normalized[1:])):
        raise ValueError("解说时间窗重叠，请调整后导入，不能让两句配音互相覆盖。")
    return dict(schema_version=1, time_basis="output", timeline_fingerprint=fingerprint, cues=normalized)


def require_current_plan(plan, cut):
    if plan.get("timeline_fingerprint") != timeline_fingerprint(cut):
        raise ValueError("剪辑顺序、切点、速度或字卡已变化，解说计划需重新对齐；未按旧时间继续处理。")


def load_narration(path, cut, *, confirm_output_time=False, original_audio_override=None):
    if original_audio_override not in (None, "keep", "mute"):
        raise ValueError("自带配音原声策略须为 keep 或 mute。")

    def validate(raw):
        if original_audio_override is not None and isinstance(raw, dict):
            for row in raw.get("cues", []) if isinstance(raw.get("cues"), list) else []:
                if isinstance(row, dict):
                    row["original_audio"] = original_audio_override
        return import_narration(raw, cut, allow_bind_current=confirm_output_time)

    path = Path(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("解说稿超过2 MiB。")
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        return validate(raw)
    if not confirm_output_time:
        raise ValueError("SRT/Markdown/TXT 无时间轴编号，须明确确认其中时间是当前成片时间。")
    if path.suffix.lower() == ".srt":
        cues = load_srt(path, source_file="output")
    elif path.suffix.lower() in (".md", ".markdown", ".txt"):
        cues = load_transcriber_markdown(path, source_file="output")
    else:
        raise ValueError("支持 JSON、SRT、Markdown 或 TXT 解说稿。")
    raw = dict(schema_version=1, time_basis="output", cues=[dict(id=f"n{i+1}", text=cue.text,
               start_ms=cue.start_us // 1000, end_ms=cue.end_us // 1000) for i, cue in enumerate(cues)])
    return validate(raw)
