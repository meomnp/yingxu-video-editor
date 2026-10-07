import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_slice_assistant.runtime_settings import runtime_setting


class RuntimeSettingsTests(unittest.TestCase):
    def test_environment_then_machine_config_then_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.yingxu').mkdir()
            config = root / '.yingxu' / 'runtime.json'
            config.write_text(json.dumps({'LOCAL_SLICE_TEST': 'machine-path'}), encoding='utf-8')
            with patch('local_slice_assistant.runtime_settings.Path.home', return_value=root), patch.dict('os.environ', {}, clear=True):
                self.assertEqual(runtime_setting('LOCAL_SLICE_TEST', 'default'), 'machine-path')
                self.assertEqual(runtime_setting('MISSING', 'default'), 'default')
                with patch.dict('os.environ', {'LOCAL_SLICE_TEST': 'environment-path'}):
                    self.assertEqual(runtime_setting('LOCAL_SLICE_TEST', 'default'), 'environment-path')
                config.write_text('invalid json', encoding='utf-8')
                self.assertEqual(runtime_setting('LOCAL_SLICE_TEST', 'default'), 'default')

    def test_non_object_or_empty_value_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.yingxu').mkdir()
            config = root / '.yingxu' / 'runtime.json'
            with patch('local_slice_assistant.runtime_settings.Path.home', return_value=root), patch.dict('os.environ', {}, clear=True):
                for value in ('[]', '{"LOCAL_SLICE_TEST": " "}', '{"LOCAL_SLICE_TEST": 42}'):
                    config.write_text(value, encoding='utf-8')
                    self.assertEqual(runtime_setting('LOCAL_SLICE_TEST', 'default'), 'default')
