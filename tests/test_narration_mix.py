from array import array
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
import wave

from local_slice_assistant.models import Cut, Segment
from local_slice_assistant.narration_plan import import_narration
from local_slice_assistant.narration_mix import mix_narration, file_sha256
from local_slice_assistant.ffmpeg import ffmpeg_binary, run_ffmpeg


def tone(path, frequency, seconds):
    samples = array("h", (int(4000 * math.sin(2 * math.pi * frequency * i / 48000)) for i in range(int(seconds * 48000))))
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 48000, 0, "NONE", "NONE"))
        output.writeframes(samples.tobytes())


class NarrationMixTests(unittest.TestCase):
    def test_mute_is_confined_to_narration_window_and_original_returns(self):
        self.plan['cues'][0]['original_audio'] = 'mute'
        output = self.root/'muted-window.wav'
        mix_narration(self.batch, self.cut, self.original, None, output)
        run = subprocess.run([ffmpeg_binary(), '-v', 'error', '-i', str(output), '-ac', '1', '-ar', '48000', '-f', 's16le', '-'], capture_output=True, check=True)
        samples = array('h', run.stdout)
        def strength(start, frequency):
            part = samples[int(start*48000):int(start*48000)+4800]
            return abs(sum(value * complex(math.cos(2*math.pi*frequency*i/48000), math.sin(2*math.pi*frequency*i/48000)) for i, value in enumerate(part))) / len(part)
        self.assertGreater(strength(.2, 1000), 1000)
        self.assertLess(strength(1.2, 1000), 20)
        self.assertGreater(strength(1.2, 600), 1000)
        self.assertLess(strength(1.7, 1000), 20)
        self.assertGreater(strength(2.2, 1000), 1000)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original, self.background, self.voice = [self.root / name for name in ("original.wav", "background.wav", "voice.wav")]
        tone(self.original, 1000, 3)
        tone(self.background, 220, 3)
        tone(self.voice, 600, .5)
        self.cut = Cut("mix", [Segment("a.mp4", 0, 3000000, "A")])
        self.plan = import_narration(dict(schema_version=1, time_basis="output", cues=[dict(id="n1", text="配音", start_ms=1000, end_ms=2000)]), self.cut, allow_bind_current=True)
        self.batch = dict(state="ready", plan=self.plan, jobs=[dict(cue_id="n1", state="ready", wav_path=str(self.voice), audio_sha256=file_sha256(self.voice))])

    def tearDown(self):
        self.temp.cleanup()

    def test_original_preserved_outside_narration(self):
        output = self.root / "mix.wav"
        mix_narration(self.batch, self.cut, self.original, self.background, output)
        def energy(start, frequency):
            run = subprocess.run([ffmpeg_binary(), "-v", "error", "-ss", str(start), "-i", str(output), "-t", "0.1", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"], capture_output=True, check=True)
            samples = array("h", run.stdout)
            return abs(sum(value * complex(math.cos(2*math.pi*frequency*i/48000), math.sin(2*math.pi*frequency*i/48000)) for i, value in enumerate(samples))) / len(samples)
        self.assertGreater(energy(.2, 1000), 1000)
        self.assertGreater(energy(2.2, 1000), 1000)
        self.assertLess(energy(1.2, 1000), 20)
        self.assertGreater(energy(1.2, 600), 1000)
        self.assertGreater(energy(1.7, 220), 100)
        self.assertLess(energy(1.7, 600), 20)

    def test_missing_stem_is_not_muting(self):
        with self.assertRaisesRegex(ValueError, "真实分离"):
            mix_narration(self.batch, self.cut, self.original, None, self.root / "mix.wav")

    def test_many_cues_use_bounded_voice_track(self):
        tone(self.voice, 600, .05)
        cues = [dict(id=f'n{i}', text='短句', start_ms=i*100, end_ms=i*100+80) for i in range(20)]
        plan = import_narration(dict(schema_version=1, time_basis='output', cues=cues), self.cut, allow_bind_current=True)
        batch = dict(state='ready', plan=plan, jobs=[dict(cue_id=c['id'], state='ready',
            wav_path=str(self.voice), audio_sha256=file_sha256(self.voice)) for c in cues])
        output = self.root / 'many.wav'
        result = mix_narration(batch, self.cut, self.original, self.background, output)
        self.assertEqual(result['duration_us'], 3000000)
        decoded = subprocess.run([ffmpeg_binary(), '-v', 'error', '-i', str(output),
            '-ac', '1', '-ar', '48000', '-f', 's16le', '-'], capture_output=True, check=True)
        samples = array('h', decoded.stdout)
        for index in (0, 8, 19):
            start = index*4800 + 480
            strength = abs(sum(value * complex(math.cos(2*math.pi*600*i/48000), math.sin(2*math.pi*600*i/48000))
                for i, value in enumerate(samples[start:start+960]))) / 960
            self.assertGreater(strength, 1000)

    def test_large_filter_graph_executes_from_script(self):
        output = self.root / 'long_graph.wav'
        graph = ' ' * 17000 + '[0:a]anull[out]'
        run_ffmpeg(['-nostdin', '-n', '-i', str(self.voice), '-filter_complex', graph,
                    '-map', '[out]', '-c:a', 'pcm_s16le', str(output)])
        with wave.open(str(output), 'rb') as reader:
            self.assertEqual(reader.getnframes(), 24000)
