import tempfile
import unittest
from pathlib import Path

from local_slice_assistant.narration_files import match_audio_files, validate_audio_filename
from local_slice_assistant.narration_plan import import_narration
from local_slice_assistant.models import Cut, Segment


class NarrationFileTests(unittest.TestCase):
    def test_exact_names_accept_alternate_format_and_never_guess_subject(self):
        cues = [dict(id='01_01', audio_filename='01_01_人物反击.wav'),
                dict(id='01_02', audio_filename='01_02_揭开前因.wav')]
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder)/name for name in ('01_01_人物反击.mp3', '人物反击.wav', '02_01_揭开前因.wav')]
            for path in paths:
                path.touch()
            matches, missing, ambiguous = match_audio_files(cues, paths)
            self.assertEqual(matches, {'01_01': str(paths[0].resolve())})
            self.assertEqual(missing, ['01_02'])
            self.assertFalse(ambiguous)
            duplicate = Path(folder)/'01_01_人物反击.wav'
            duplicate.touch()
            matches, _, ambiguous = match_audio_files(cues, [*paths, duplicate])
            self.assertNotIn('01_01', matches)
            self.assertEqual(ambiguous, ['01_01'])

    def test_names_cannot_be_paths_or_unsupported_files(self):
        for name in ('../voice.wav', 'D:\\voice.wav', 'a/b.wav', 'CON.wav', 'voice.exe', 'voice?.mp3', ' voice.wav'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_audio_filename(name)

    def test_named_cue_survives_import_and_duplicate_names_are_rejected(self):
        cut = Cut('解说', [Segment('a.mp4', 0, 40_000_000, '原片')])
        raw = dict(schema_version=1, time_basis='output', cues=[dict(id='01_01', audio_filename='01_01_人物反击.wav',
                   text='解说词', start_ms=20000, end_ms=30000, original_audio='mute')])
        plan = import_narration(raw, cut, allow_bind_current=True)
        self.assertEqual(plan['cues'][0]['audio_filename'], '01_01_人物反击.wav')
        self.assertEqual((plan['cues'][0]['start_us'], plan['cues'][0]['end_us']), (20_000_000, 30_000_000))
        raw['cues'].append(dict(raw['cues'][0], id='01_02', start_ms=30000, end_ms=35000))
        with self.assertRaisesRegex(ValueError, '不能重复'):
            import_narration(raw, cut, allow_bind_current=True)
