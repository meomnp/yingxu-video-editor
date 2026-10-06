from pathlib import Path
from threading import Event
import tempfile
import unittest
from unittest.mock import patch

from local_slice_assistant.ffmpeg import run_ffmpeg
from local_slice_assistant.manifest import create_project_from_video
from local_slice_assistant.manual_narration import prepare_manual_narration
from local_slice_assistant.models import Cut, Segment
from local_slice_assistant.narration_plan import import_narration, load_narration
from local_slice_assistant.narration_mix import wav_duration, file_sha256
from local_slice_assistant.exporter import export_cut, ExportSettings
from local_slice_assistant.project_store import save_project, load_project
from tests.test_narration_mix import tone
from tests.test_stage1_media_integration import _audio_at


class ManualNarrationTests(unittest.TestCase):
    def test_srt_strategy_is_validated_before_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'voice.srt'
            path.write_text('1\n00:00:00,000 --> 00:00:01,000\n测试\n', encoding='utf-8')
            cut = Cut('test', [Segment('a.mp4', 0, 3000000, 'a', original_audio='mute')])
            plan = load_narration(path, cut, confirm_output_time=True, original_audio_override='mute')
            self.assertEqual(plan['cues'][0]['original_audio'], 'mute')
            with self.assertRaisesRegex(ValueError, '已静音'):
                load_narration(path, cut, confirm_output_time=True, original_audio_override='keep')
            with self.assertRaisesRegex(ValueError, '须明确确认'):
                load_narration(path, cut, original_audio_override='mute')

    def test_real_audio_matching_and_rejections_without_voice_service(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / 'source.mp4'
            run_ffmpeg(['-nostdin', '-n', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=25:d=3',
                        '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000:duration=3',
                        '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(video)])
            document = create_project_from_video(video)
            cut = document.active_cut
            plan = import_narration(dict(schema_version=1, time_basis='output', cues=[
                dict(id='n1', text='测试', start_ms=1000, end_ms=2000, original_audio='keep')]),
                cut, allow_bind_current=True)
            document.replace_packaging(cut.id, {'narration_plan': plan}, 'test')
            voice = root / 'voice.wav'
            tone(voice, 600, .5)
            original_hashes = file_sha256(video), file_sha256(voice)
            with patch('local_slice_assistant.narration_pipeline.run_batch') as tts, \
                 patch('local_slice_assistant.narration_pipeline.separate_audio') as separator:
                render = prepare_manual_narration(document, cut.id, {'n1': voice}, lambda _: None, Event())
                self.assertEqual(wav_duration(render['path']), 3000000)
                self.assertEqual(render['sha256'], file_sha256(render['path']))
                self.assertEqual(original_hashes, (file_sha256(video), file_sha256(voice)))
                document.replace_packaging(cut.id, {'narration_plan': plan, 'narration_render': render}, 'apply')
                reopened = load_project(save_project(document, root / 'manual.localcut.json')).document
                output = export_cut(reopened, settings=ExportSettings(include_packaging=True))
                self.assertAlmostEqual(output.observed_duration_us, 3000000, delta=40000)
                # The imported voice begins at output 1s, not at timeline zero.
                self.assertAlmostEqual(_audio_at(output.output_path, .2)[0], 1000, delta=35)
                self.assertAlmostEqual(_audio_at(output.output_path, 1.1)[0], 600, delta=35)
                self.assertAlmostEqual(_audio_at(output.output_path, 2.2)[0], 1000, delta=35)
                self.assertEqual(original_hashes, (file_sha256(video), file_sha256(voice)))
                before = document.to_dict()
                with self.assertRaisesRegex(ValueError, '漏句'):
                    prepare_manual_narration(document, cut.id, {}, lambda _: None, Event())
                tone(voice, 600, 1.5)
                with self.assertRaisesRegex(ValueError, '超过指定'):
                    prepare_manual_narration(document, cut.id, {'n1': voice}, lambda _: None, Event())
                tts.assert_not_called()
                separator.assert_not_called()
            self.assertEqual(document.to_dict(), before)
