from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from local_slice_assistant.cli import main
from local_slice_assistant.manifest import import_manifest
from local_slice_assistant.narration_automation import automate
from local_slice_assistant.narration_batch import create_batch
from local_slice_assistant.narration_batch import save_batch
from local_slice_assistant.narration_pipeline import produce_narration
from local_slice_assistant.narration_plan import import_narration
from local_slice_assistant.project_store import save_project, load_project
from tests.helpers import make_empty_sources, write_manifest, patched_probe


class NarrationAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.probe = patched_probe()
        self.probe.__enter__()
        self.addCleanup(self.probe.__exit__, None, None, None)
        make_empty_sources(self.root)
        self.doc = import_manifest(write_manifest(self.root), self.root)
        self.cut = self.doc.active_cut
        self.plan = import_narration(dict(schema_version=1, time_basis='output', cues=[
            dict(id='n1', text='测试', start_ms=0, end_ms=1000)]), self.cut, allow_bind_current=True)
        self.cut.packaging['narration_plan'] = self.plan
        self.project = save_project(self.doc, self.root / 'input.localcut.json')
        self.target = self.root / 'result.localcut.json'

    def test_cli_preflight_has_no_writes_or_service_calls(self):
        before = self.project.read_bytes()
        with patch('local_slice_assistant.narration_automation.produce_narration') as produce, \
             redirect_stdout(StringIO()) as output:
            code = main(['narration-run', '--project', str(self.project), '--cut', self.cut.id,
                         '--result-project', str(self.target), '--profile', 'authorized-profile'])
        self.assertEqual(code, 0)
        self.assertIn('preflight', output.getvalue())
        produce.assert_not_called()
        self.assertFalse(self.target.exists())
        self.assertFalse((self.root / '.local_slice_assistant' / 'narration').exists())
        self.assertEqual(self.project.read_bytes(), before)

    def test_incomplete_preserves_batch_and_never_saves_result(self):
        def incomplete(document, cut_id, batch, progress, cancel):
            batch['state'] = 'incomplete'
            return batch
        with patch('local_slice_assistant.narration_automation.produce_narration', side_effect=incomplete):
            result = automate(self.project, self.cut.id, self.target, lambda _: None, Event(),
                              execute=True, profile_id='authorized-profile')
        self.assertEqual(result['state'], 'incomplete')
        self.assertTrue(Path(result['batch']).is_file())
        self.assertFalse(self.target.exists())

    def test_existing_result_rejected_before_work(self):
        self.target.write_text('preserve')
        with self.assertRaisesRegex(ValueError, '新文件'):
            automate(self.project, self.cut.id, self.target, print, Event(), execute=True, profile_id='p')
        self.assertEqual(self.target.read_text(), 'preserve')

    def test_execution_lock_blocks_duplicate_and_is_preserved(self):
        batch = create_batch(self.plan, self.cut, self.root / 'jobs', 'p')
        lock = Path(batch['directory']) / 'production.lock'
        lock.write_text('other worker')
        with patch('local_slice_assistant.narration_pipeline._produce_narration') as produce:
            with self.assertRaisesRegex(ValueError, '执行锁'):
                produce_narration(self.doc, self.cut.id, batch, print, Event())
            produce.assert_not_called()
        self.assertEqual(lock.read_text(), 'other worker')

    def test_lock_released_after_failure(self):
        batch = create_batch(self.plan, self.cut, self.root / 'jobs', 'p')
        with patch('local_slice_assistant.narration_pipeline._produce_narration', side_effect=ValueError('failure')):
            with self.assertRaisesRegex(ValueError, 'failure'):
                produce_narration(self.doc, self.cut.id, batch, print, Event())
        self.assertFalse((Path(batch['directory']) / 'production.lock').exists())

    def test_batch_reloaded_under_lock_instead_of_resubmitting_stale_pending(self):
        from copy import deepcopy
        stale = create_batch(self.plan, self.cut, self.root / 'jobs', 'p')
        current = deepcopy(stale)
        current['jobs'][0]['state'] = 'submitted'
        save_batch(current)
        with patch('local_slice_assistant.narration_pipeline._produce_narration') as produce:
            produce_narration(self.doc, self.cut.id, stale, print, Event())
            self.assertEqual(produce.call_args.args[2]['jobs'][0]['state'], 'submitted')
