from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from local_slice_assistant.ffmpeg import run_ffmpeg
from local_slice_assistant.manifest import create_project_from_video, import_manifest
from local_slice_assistant.narration_plan import import_narration, require_current_plan
from local_slice_assistant.narration_batch import create_batch
from local_slice_assistant.narration_pipeline import produce_narration, load_batch, extract_original_audio
from local_slice_assistant.narration_mix import file_sha256, wav_duration, apply_narration_render
from local_slice_assistant.project_store import save_project, load_project
from tests.test_narration_mix import tone
from tests.helpers import standard_manifest, write_manifest, make_empty_sources, patched_probe


class NarrationPipelineTests(unittest.TestCase):
    def test_source_changes_during_extraction_or_separation_stop_next_stage(self):
        for stage in ('extraction', 'separation'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder, patched_probe():
                root = Path(folder)
                make_empty_sources(root)
                document = import_manifest(write_manifest(root), root)
                cut = document.active_cut
                plan = import_narration(dict(schema_version=1, time_basis='output', cues=[
                    dict(id='n1', text='测试', start_ms=0, end_ms=1000,
                         original_audio='remove_dialogue')]), cut, allow_bind_current=True)
                batch = create_batch(plan, cut, root / 'jobs', 'test-profile')

                def mutate():
                    (root / '19.mp4').write_bytes(b'changed during ' + stage.encode())

                def extract(document, cut, output, progress, cancel):
                    tone(output, 200, 1)
                    if stage == 'extraction':
                        mutate()

                def separate(original, directory, progress, cancel):
                    mutate()
                    return {'background_path': original}

                with patch('local_slice_assistant.narration_pipeline.extract_original_audio', side_effect=extract), \
                     patch('local_slice_assistant.narration_pipeline.separate_audio', side_effect=separate) as separator, \
                     patch('local_slice_assistant.narration_pipeline.run_batch') as voices:
                    with self.assertRaisesRegex(ValueError, '原视频已变化'):
                        produce_narration(document, cut.id, batch, lambda _: None, Event(),
                                          separator=separator, batch_runner=voices)
                    voices.assert_not_called()
                    self.assertEqual(separator.call_count, int(stage == 'separation'))
                self.assertEqual(batch['state'], 'incomplete')
                self.assertNotIn('render', batch)
                if stage == 'extraction':
                    self.assertFalse((Path(batch['directory']) / 'original.wav').exists())

    def test_changed_source_before_first_run_stops_before_any_media_or_voice_work(self):
        with tempfile.TemporaryDirectory() as folder, patched_probe():
            root = Path(folder)
            make_empty_sources(root)
            document = import_manifest(write_manifest(root), root)
            cut = document.active_cut
            plan = import_narration(dict(schema_version=1, time_basis='output', cues=[
                dict(id='n1', text='测试', start_ms=0, end_ms=1000)]), cut, allow_bind_current=True)
            batch = create_batch(plan, cut, root / 'jobs', 'test-profile')
            (root / '19.mp4').write_bytes(b'changed before first production')
            with patch('local_slice_assistant.narration_pipeline.extract_original_audio') as extract, \
                 patch('local_slice_assistant.narration_pipeline.separate_audio') as separate, \
                 patch('local_slice_assistant.narration_pipeline.run_batch') as voices:
                with self.assertRaisesRegex(ValueError, '工程原视频已变化'):
                    produce_narration(document, cut.id, batch, lambda _: None, Event(), separator=separate, batch_runner=voices)
                extract.assert_not_called()
                separate.assert_not_called()
                voices.assert_not_called()
            self.assertEqual(batch['state'], 'incomplete')
            self.assertIn('已变化', batch['error'])

    def test_cancel_before_start_does_not_read_sources_or_extract(self):
        with tempfile.TemporaryDirectory() as folder, patched_probe():
            root = Path(folder)
            make_empty_sources(root)
            document = import_manifest(write_manifest(root), root)
            cut = document.active_cut
            plan = import_narration(dict(schema_version=1, time_basis='output', cues=[
                dict(id='n1', text='测试', start_ms=0, end_ms=1000)]), cut, allow_bind_current=True)
            batch = create_batch(plan, cut, root / 'jobs', 'test-profile')
            cancel = Event()
            cancel.set()
            with patch('local_slice_assistant.narration_pipeline.source_identity') as identity, \
                 patch('local_slice_assistant.narration_pipeline.extract_original_audio') as extract:
                with self.assertRaisesRegex(ValueError, '已停止'):
                    produce_narration(document, cut.id, batch, lambda _: None, cancel)
                identity.assert_not_called()
                extract.assert_not_called()
            self.assertEqual(batch['state'], 'stopped')

    def test_resampled_original_audio_matches_exact_picture_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / 'resample.mp4'
            run_ffmpeg(['-nostdin', '-n', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=25:d=16',
                        '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=22050:duration=16',
                        '-c:v', 'libx264', '-c:a', 'aac', '-t', '16', str(video)])
            document = create_project_from_video(video)
            output = root / 'original.wav'
            extract_original_audio(document, document.active_cut, output, lambda _: None, Event())
            self.assertEqual(wav_duration(output), 16000000)

    def test_embedded_plan_maps_across_sources_and_survives_save(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_empty_sources(root)
            payload = standard_manifest()
            payload['cuts'][0]['narration'] = dict(schema_version=1, time_basis='output', cues=[
                dict(id='intro', text='跨镜头解说', start_ms=7000, end_ms=9000)])
            with patched_probe():
                document = import_manifest(write_manifest(root, payload), root)
                cut = document.active_cut
                plan = cut.packaging['narration_plan']
                self.assertEqual(len(plan['cues'][0]['spans']), 2)
                target = root / 'project.localcut.json'
                save_project(document, target)
                reopened = load_project(target).document.active_cut
                require_current_plan(reopened.packaging['narration_plan'], reopened)
                self.assertEqual(plan, reopened.packaging['narration_plan'])

    def test_real_extraction_mix_resume_and_source_change_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / 'source.mp4'
            run_ffmpeg(['-nostdin', '-n', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=25:d=3',
                        '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000:duration=3',
                        '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(video)])
            document = create_project_from_video(video)
            cut = document.active_cut
            plan = import_narration(dict(schema_version=1, time_basis='output', cues=[
                dict(id='n1', text='测试配音', start_ms=1000, end_ms=2000, original_audio='keep')]),
                cut, allow_bind_current=True)
            batch = create_batch(plan, cut, root / 'jobs', 'test-profile')
            calls = []
            def voices(batch, cut, progress, cancel):
                for job in batch['jobs']:
                    if job['state'] != 'ready':
                        calls.append(job['cue_id'])
                        path = Path(batch['directory']) / 'voice.wav'
                        tone(path, 600, .5)
                        job.update(state='ready', wav_path=str(path), audio_sha256=file_sha256(path))
                batch['state'] = 'ready'
            produce_narration(document, cut.id, batch, lambda _: None, Event(), batch_runner=voices)
            self.assertEqual(batch['phase'], '制作完成')
            self.assertAlmostEqual(wav_duration(batch['render']['path']), 3000000, delta=1000)
            loaded = load_batch(Path(batch['directory']) / 'batch.json', cut, plan)
            produce_narration(document, cut.id, loaded, lambda _: None, Event(), batch_runner=voices)
            self.assertEqual(calls, ['n1'])
            cut.packaging.update(narration_plan=plan, narration_render=loaded['render'])
            self.assertEqual(apply_narration_render([], [], '[a]', cut, document), '[narration_base]')
            # If media changes while the voice service runs, do not mix/attach it.
            def mutate_source(batch, cut, progress, cancel):
                with video.open('ab') as stream:
                    stream.write(b'mid-production mutation')
                batch['state'] = 'ready'
            with patch('local_slice_assistant.narration_pipeline.mix_narration') as mix:
                with self.assertRaisesRegex(ValueError, '原视频已变化'):
                    produce_narration(document, cut.id, loaded, lambda _: None, Event(), batch_runner=mutate_source)
                mix.assert_not_called()
            cut.packaging.update(narration_plan=plan, narration_render=loaded['render'])
            with self.assertRaisesRegex(ValueError, '原素材已变化'):
                apply_narration_render([], [], '[a]', cut, document)
            with self.assertRaisesRegex(ValueError, '原视频已变化'):
                produce_narration(document, cut.id, loaded, lambda _: None, Event(), batch_runner=voices)
