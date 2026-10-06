"""Render the production graph to lossless PCM; verify gains and sample boundaries."""
from array import array
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import wave

from local_slice_assistant.exporter import _filter_graph
from local_slice_assistant.ffmpeg import run_ffmpeg
from local_slice_assistant.manifest import import_manifest
from local_slice_assistant.project_store import save_project, load_project


def constant_wav(path, samples, left, right):
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, 48000, 0, "NONE", "NONE"))
        output.writeframes(array("h", [left, right] * samples).tobytes())


class AudioPackagingPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bed = self.root / "原声.wav"
        constant_wav(bed, 144000, 4000, 8000)
        self.source = self.root / "源片.mkv"
        run_ffmpeg(["-nostdin", "-n", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=25:d=3",
                    "-i", str(bed), "-map", "0:v", "-map", "1:a", "-c:v", "ffv1",
                    "-c:a", "pcm_s16le", str(self.source)])
        manifest = self.root / "plan.json"
        manifest.write_text(json.dumps(dict(schema_version=1, example_only=False, drama="声音精度", sources=[
            dict(file=self.source.name, episode=1, expected_duration_ms=3000)], cuts=[dict(
                title="测试", segments=[dict(file=self.source.name, in_ms=0, out_ms=3000, purpose="合成夹具")])])), encoding="utf-8")
        self.doc = import_manifest(manifest, self.root)
        self.cut = self.doc.active_cut
        self.voice = self.root / "配音.wav"
        constant_wav(self.voice, 9612, 1000, 2000)
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def item(self, identity, start, *, mute=False, volume=1.0):
        return dict(id=identity, kind="voiceover" if mute else "music", file_path=str(self.voice),
                    anchor_segment_id=self.cut.segments[0].id, source_offset_us=start,
                    source_in_us=0, duration_us=200250, enabled=True,
                    mute_original=mute, volume=volume, script="合成测试")

    def render(self, name):
        output = self.root / name
        inputs, graph = _filter_graph(self.doc, self.cut, width=160, height=90,
                                     fps_num=25, fps_den=1, include_packaging=True)
        run_ffmpeg(["-nostdin", "-n", *inputs, "-filter_complex", graph, "-map", "[aout]",
                    "-c:a", "pcm_s16le", str(output), "-map", "[vout]", "-f", "null", os.devnull])
        with wave.open(str(output), "rb") as reader:
            self.assertEqual(reader.getnframes(), round(self.doc.total_duration_us() * 48000 / 1000000))
            self.assertEqual(reader.getnchannels(), 2)
            samples = array("h", reader.readframes(reader.getnframes()))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)
        return samples

    def test_delayed_items_do_not_attenuate_bed_or_jump_when_other_items_end(self):
        baseline = self.render("baseline.wav")
        packaging = deepcopy(self.cut.packaging)
        packaging["audio_items"] = [self.item("a", 1000125), self.item("b", 2000125, volume=.5)]
        self.doc.replace_packaging(self.cut.id, packaging, "加入声音")
        result = self.render("mixed.wav")
        for sample, expected_extra in ((24000, 0), (52000, 1000), (70000, 0), (100000, 500), (120000, 0)):
            self.assertAlmostEqual(result[sample * 2], baseline[sample * 2] + expected_extra, delta=1)
            self.assertAlmostEqual(result[sample * 2 + 1], baseline[sample * 2 + 1] + expected_extra * 2, delta=1)
        project = save_project(self.doc, self.root / "mix.localcut.json")
        self.doc = load_project(project).document
        self.cut = self.doc.active_cut
        self.assertEqual(result, self.render("reopened.wav"))
        self.doc.undo()
        self.assertEqual(baseline, self.render("undo.wav"))

    def test_voice_and_original_mute_share_exact_half_open_sample_window(self):
        baseline = self.render("baseline.wav")
        packaging = deepcopy(self.cut.packaging)
        # Deliberately not aligned to whole milliseconds or FFmpeg audio blocks.
        packaging["audio_items"] = [self.item("voice", 1000125, mute=True)]
        self.doc.replace_packaging(self.cut.id, packaging, "加入旁白")
        result = self.render("voice.wav")
        for sample in range(47000, 58500):
            for channel, level in enumerate((1000, 2000)):
                expected = level if 48006 <= sample < 57618 else baseline[sample * 2 + channel]
                self.assertAlmostEqual(result[sample * 2 + channel], expected, delta=1,
                                       msg=f"sample={sample}, channel={channel}")

    def test_trimmed_video_moves_voice_and_mute_together_without_drifting(self):
        self.doc.adjust_segment_start(self.cut.id, 0, 250000)
        baseline = self.render("trimmed-original.wav")
        self.doc.undo()
        packaging = deepcopy(self.cut.packaging)
        packaging["audio_items"] = [self.item("voice", 1000125, mute=True)]
        self.doc.replace_packaging(self.cut.id, packaging, "加入旁白")
        self.doc.adjust_segment_start(self.cut.id, 0, 250000)
        result = self.render("trimmed.wav")
        for sample in (36005, 36006, 45617, 45618, 48006):
            voice_present = 36006 <= sample < 45618
            for channel in (0, 1):
                expected = 1000 * (channel + 1) if voice_present else baseline[sample * 2 + channel]
                self.assertAlmostEqual(result[sample * 2 + channel], expected, delta=2)

    def test_overlapping_mute_windows_keep_both_voices_without_dropping_channels(self):
        baseline = self.render("baseline.wav")
        packaging = deepcopy(self.cut.packaging)
        packaging["audio_items"] = [self.item("a", 1000125, mute=True), self.item("b", 1100125, mute=True)]
        self.doc.replace_packaging(self.cut.id, packaging, "两句交叠")
        result = self.render("overlap.wav")
        for sample in (48005, 48006, 52805, 52806, 57617, 57618, 62417, 62418):
            count = int(48006 <= sample < 57618) + int(52806 <= sample < 62418)
            for channel, level in enumerate((1000, 2000)):
                expected = count * level if count else baseline[sample * 2 + channel]
                self.assertAlmostEqual(result[sample * 2 + channel], expected, delta=1)

    def test_loud_overlap_is_limited_without_changing_duration(self):
        constant_wav(self.voice, 9612, 25000, 26000)
        packaging = deepcopy(self.cut.packaging)
        packaging["audio_items"] = [self.item("a", 1000000), self.item("b", 1000000)]
        self.doc.replace_packaging(self.cut.id, packaging, "测试削峰")
        result = self.render("loud.wav")
        peak = max(abs(sample) for sample in result)
        self.assertGreater(peak, 28000)
        self.assertLessEqual(peak, 31131)

    def test_22050_mono_voice_resamples_into_same_exact_mute_window(self):
        # Matches the voice service's sample-rate/channel format without TTS.
        baseline = self.render("baseline.wav")
        frame_count = 4414
        with wave.open(str(self.voice), "wb") as output:
            output.setparams((1, 2, 22050, 0, "NONE", "NONE"))
            output.writeframes(array("h", [4000] * frame_count).tobytes())
        item = self.item("resampled", 1000125, mute=True)
        item["duration_us"] = (frame_count * 1000000 + 11025) // 22050
        packaging = deepcopy(self.cut.packaging)
        packaging["audio_items"] = [item]
        self.doc.replace_packaging(self.cut.id, packaging, "低采样率单声道")
        reference = self.root / "reference.wav"
        run_ffmpeg(["-nostdin", "-n", "-i", str(self.voice), "-af",
                    "aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo",
                    "-c:a", "pcm_s16le", str(reference)])
        with wave.open(str(reference), "rb") as reader:
            voice = array("h", reader.readframes(reader.getnframes()))
        result = self.render("resampled.wav")
        end_sample = ((item["source_offset_us"] + item["duration_us"]) * 48000 + 500000) // 1000000
        for sample in range(47900, end_sample + 100):
            for channel in range(2):
                index = (sample - 48006) * 2 + channel
                expected = (voice[index] if index < len(voice) else 0) if 48006 <= sample < end_sample else baseline[sample * 2 + channel]
                self.assertAlmostEqual(result[sample * 2 + channel], expected, delta=2,
                                       msg=f"sample={sample}, channel={channel}")
