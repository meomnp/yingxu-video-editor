import unittest
from unittest.mock import patch
import local_slice_assistant_app as entry


class ReleaseEntryTests(unittest.TestCase):
    def test_console_executable_uses_cli_without_gui(self):
        with patch.object(entry.sys, 'executable', 'D:/release/LocalSliceAssistantCLI.exe'), \
             patch.object(entry.sys, 'argv', ['LocalSliceAssistantCLI.exe', '--help']), \
             patch('local_slice_assistant.cli.main', return_value=0) as cli:
            self.assertEqual(entry.main(), 0)
            cli.assert_called_once_with(['--help'])

    def test_source_explicit_cli_dispatch(self):
        with patch.object(entry.sys, 'argv', ['app.py', '--cli', 'inspect', '--project', 'input.json']), \
             patch('local_slice_assistant.cli.main', return_value=2) as cli:
            self.assertEqual(entry.main(), 2)
            cli.assert_called_once_with(['inspect', '--project', 'input.json'])
