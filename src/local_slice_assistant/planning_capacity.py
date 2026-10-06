"""Measured text volume, not a tokenizer or a provider pricing estimate."""
import json


def capacity_summary(package, messages=None):
    cues = package['timestamped_transcript']
    body = (json.dumps(package, ensure_ascii=False) if messages is None else
            '\n'.join(message['content'] for message in messages))
    characters = len(body)
    missing = sum(not source['has_timestamped_transcript'] for source in package['source_catalog'])
    report = (f"本批 {len(package['source_catalog']):,} 个视频 · {len(cues):,} 条台词 · "
              f"台词 {sum(len(cue['text']) for cue in cues):,} 字符\n"
              f"{'任务包' if messages is None else '本次全部消息'} {characters:,} 字符；"
              "字符数不是 token 数，也不是费用估算。")
    if missing:
        report += f'\n其中 {missing} 个视频没有带时间戳台词，AI 无法据此理解其剧情。'
    if characters > 100_000:
        report += ('\n长文本提醒：请先核对所选模型的上下文容量与费用，必要时用较小素材范围另建工程。'
                   '不会自动截断台词或拆分成多次计费请求。')
    return report
