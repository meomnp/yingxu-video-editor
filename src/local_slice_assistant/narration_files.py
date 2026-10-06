"""Deterministic filenames for user-supplied narration; never guess by dialogue."""
from pathlib import Path
import re
import unicodedata

AUDIO_SUFFIXES = {'.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg'}


def validate_audio_filename(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 160
            or any(ord(char) < 32 for char in value) or re.search(r'[<>:"/\\|?*]', value)
            or value != value.strip() or value.endswith(('.', ' '))
            or Path(value).suffix.casefold() not in AUDIO_SUFFIXES
            or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', value.split('.')[0])):
        raise ValueError('解说音频文件名须为普通音频文件名，不能包含路径或特殊字符。')
    return value


def expected_audio_filename(cue):
    explicit = cue.get('audio_filename')
    if explicit:
        return validate_audio_filename(explicit)
    # Older imported plans may use arbitrary cue IDs; retain a safe fallback.
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', cue['id']).strip(' .')[:100] or '解说'
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', stem):
        stem = '_' + stem
    return stem + '.wav'


def match_audio_files(cues, files):
    def key(value):
        return unicodedata.normalize('NFC', Path(value).stem).casefold()
    candidates = {}
    for file in files:
        path = Path(file)
        if path.suffix.casefold() in AUDIO_SUFFIXES and path.is_file():
            candidates.setdefault(key(path.name), []).append(str(path.resolve()))
    matches, missing, ambiguous = {}, [], []
    expected = {}
    for cue in cues:
        expected.setdefault(key(expected_audio_filename(cue)), []).append(cue['id'])
    for cue in cues:
        name = key(expected_audio_filename(cue))
        found = candidates.get(name, [])
        if len(found) > 1 or len(expected[name]) > 1:
            ambiguous.append(cue['id'])
        elif found:
            matches[cue['id']] = found[0]
        else:
            missing.append(cue['id'])
    return matches, missing, ambiguous


def narration_preparation_text(document):
    from .timeline import format_timecode_us
    rows = ['# 解说音频准备清单', '', '按下面的文件名准备音频，再在“字幕与包装”选择对应切片、匹配音频文件夹。',
            '时间为每条成片从零开始的位置。可使用同名 MP3 等支持格式；同名不同格式同时存在时须手动选择。',
            '实验功能：不分离人声。静音会同时去掉原片背景音乐；建议在专业剪辑软件中完成精细解说二创。', '']
    for cut_index, cut in enumerate(document.cuts, 1):
        plan = cut.packaging.get('narration_plan', {})
        if not plan.get('cues'):
            continue
        rows += [f'## {cut_index:02d} · {cut.title}', '']
        for cue in plan['cues']:
            rows += [f"### {cue['id']} · {expected_audio_filename(cue)}",
                     f"成片时间：{format_timecode_us(cue['start_us'])} → {format_timecode_us(cue['end_us'])}",
                     f"原声：{'静音（包括背景音乐）' if cue['original_audio'] == 'mute' else '降低全部原声'}；窗口结束后恢复原片声轨。",
                     f"解说词：{cue['text']}", '']
    return '\n'.join(rows)
