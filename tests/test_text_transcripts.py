import tempfile
import unittest
from pathlib import Path

from local_slice_assistant.transcripts import load_transcript, load_transcript_files, transcript_needs_source_binding
from local_slice_assistant.errors import ManifestValidationError


class TextTranscriptTests(unittest.TestCase):
    def test_merged_srt_metadata_binds_sources_without_rendering_headers(self):
        content = "1\n00:00:00,000 --> 00:00:00,001\n【《测试》｜音视频数量 2｜全部完整时长 00:02:00.000｜以下时间码均为各自源文件原始时间】\n\n"
        for index, name in enumerate(("1.mp4", "子目录/2.mp4")):
            content += f"{index * 2 + 2}\n00:00:00,000 --> 00:00:00,001\n【第{index + 1}集｜{name}｜完整时长 00:01:00.000】\n\n{index * 2 + 3}\n00:00:01,200 --> 00:00:02,300\n台词\n\n"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "合并.srt"
            path.write_text(content, encoding="utf-8-sig")
            self.assertFalse(transcript_needs_source_binding(path))
            cues = load_transcript(path)
            self.assertEqual([c.source_file for c in cues], ["1.mp4", "子目录/2.mp4"])
            self.assertTrue(all(c.start_us == 1200000 and c.text == "台词" for c in cues))
            with self.assertRaisesRegex(ManifestValidationError, "绑定不同"):
                load_transcript(path, source_file="1.mp4")

    def test_transcriber_timed_txt_chapters(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "合并.txt"
            path.write_text("生成时间：2026-10-05 10:00:00\n第1集｜1.mp4\n完整时长：00:01:00.000\n[00:00:01.200 → 00:00:02.300] 第一集\n第2个文件｜2.mp4\n[00:00:01.200 → 00:00:02.300] 第二集", encoding="utf-8")
            self.assertFalse(transcript_needs_source_binding(path))
            self.assertEqual([c.source_file for c in load_transcript(path)], ["1.mp4", "2.mp4"])
            path.write_text("第1集｜1.mp4\n只有文字，没有时间", encoding="utf-8")
            with self.assertRaises(ManifestValidationError):
                load_transcript(path)

    def test_merged_reading_export_preserves_episode_local_times(self):
        content = '''# 台词稿
- 生成时间：2026-10-04 22:25:02
- 全部音视频总时长：**00:02:00.000**
- 时间码说明：每集从 00:00:00 开始。
## 文件与完整时长
| 顺序 | 集数／文件 | 源文件 | 完整时长 | 时长依据 |
|---:|---|---|---:|---|
| 1 | 第1个文件 | `1.mp4` | 00:01:00.000 | 媒体容器 |
| 2 | 第2个文件 | `2.mp4` | 00:01:00.000 | 媒体容器 |
## 第1个文件
源文件：`1.mp4`
完整时长：`00:01:00.000`
- **00:00:01.200–00:00:02.300** 第一集台词
## 第2个文件
源文件：`2.mp4`
完整时长：`00:01:00.000`
- **00:00:01.200–00:00:02.300** 第二集台词
'''
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "合并文字稿.md"
            path.write_text(content, encoding="utf-8-sig")
            self.assertFalse(transcript_needs_source_binding(path))
            cues = load_transcript_files([str(path)], {})
            self.assertEqual([c.source_file for c in cues], ["1.mp4", "2.mp4"])
            self.assertEqual([(c.start_us, c.end_us) for c in cues],
                             [(1200000, 2300000), (1200000, 2300000)])
            path.write_text(content + "\n- **00:00:03.000** 缺少结束时间", encoding="utf-8")
            with self.assertRaises(ManifestValidationError):
                load_transcript(path)

    def test_binding_is_explicit_and_survives_batch_loading(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "新导出.txt"
            path.write_text("00:00:01 --> 00:00:02 台词", encoding="utf-8")
            self.assertTrue(transcript_needs_source_binding(path))
            with self.assertRaises(ManifestValidationError):
                load_transcript(path)
            cues = load_transcript_files([str(path)], {}, source_bindings={str(path): "子目录/01.mp4"})
            self.assertEqual(cues[0].source_file, "子目录/01.mp4")
            path.write_text("源文件：02.mp4\n00:00:01 --> 00:00:02 台词", encoding="utf-8")
            self.assertFalse(transcript_needs_source_binding(path))
            with self.assertRaisesRegex(ManifestValidationError, "绑定不同"):
                load_transcript(path, source_file="01.mp4")

    def test_srt_never_silently_drops_bad_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "台词.srt"
            for content in ("", "1\n台词", "1\n00:00:02 --> 00:00:01\n台词",
                            "1\n00:00:01 --> 00:00:02\n台词\n\n2\n缺少时间\n台词"):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ManifestValidationError):
                    load_transcript(path, source_file="01.mp4")

    def test_duplicate_exports_are_not_duplicate_subtitles(self):
        with tempfile.TemporaryDirectory() as root:
            md = Path(root) / "台词.md"
            srt = Path(root) / "台词.srt"
            md.write_text("源文件：01.mp4\n00:00:01 --> 00:00:02 台词", encoding="utf-8")
            srt.write_text("1\n00:00:01 --> 00:00:02\n台词", encoding="utf-8")
            cues = load_transcript_files([str(md), str(srt)], {}, source_bindings={str(srt): "01.mp4"})
            self.assertEqual(len(cues), 1)
            self.assertEqual(cues[0].source_kind, "srt")
            srt.write_text("1\n00:00:01 --> 00:00:02\n冲突文本", encoding="utf-8")
            with self.assertRaisesRegex(ManifestValidationError, "不同台词"):
                load_transcript_files([str(md), str(srt)], {}, source_bindings={str(srt): "01.mp4"})

    def test_wrong_encoding_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "台词.txt"
            path.write_bytes("台词".encode("gbk"))
            with self.assertRaisesRegex(ManifestValidationError, "UTF-8"):
                load_transcript(path)

    def test_txt_md_and_srt_equivalent_times(self):
        with tempfile.TemporaryDirectory() as root:
            for suffix, content in (
                (".txt", "源文件：第01集.mp4\n00:00:01.200 --> 00:00:02.300 台词"),
                (".md", "- 源文件：`第01集.mp4`\n- **00:00:01.200–00:00:02.300** 台词"),
                (".srt", "1\n00:00:01,200 --> 00:00:02,300\n台词\n"),
            ):
                path = Path(root) / ("台词" + suffix)
                path.write_text(content, encoding="utf-8")
                cue = load_transcript(path, source_file="第01集.mp4")[0]
                self.assertEqual((cue.source_file, cue.start_us, cue.end_us, cue.text),
                                 ("第01集.mp4", 1200000, 2300000, "台词"))

    def test_incomplete_or_invalid_text_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "台词.txt"
            for line in ("00:00:01.000 台词", "00:61:01 --> 00:62:02 台词",
                         "00:00:02 --> 00:00:01 台词", "没有时间"):
                path.write_text("源文件：第01集.mp4\n" + line, encoding="utf-8")
                with self.assertRaises(ManifestValidationError):
                    load_transcript(path)
