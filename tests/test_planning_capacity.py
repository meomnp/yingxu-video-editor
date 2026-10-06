import unittest
from local_slice_assistant.planning_capacity import capacity_summary
from local_slice_assistant.ai_provider import ProviderConfig, request_size_bytes, _payload


class PlanningCapacityTests(unittest.TestCase):
    def test_measures_unicode_and_missing_transcripts_without_token_claims(self):
        package = dict(source_catalog=[{'has_timestamped_transcript': True},
                                      {'has_timestamped_transcript': False}],
                       timestamped_transcript=[{'text': '你好🙂'}])
        report = capacity_summary(package)
        self.assertIn('2 个视频', report)
        self.assertIn('台词 3 字符', report)
        self.assertIn('1 个视频没有', report)
        self.assertIn('不是 token', report)
        self.assertNotIn('长文本提醒', report)

    def test_second_stage_design_counts_toward_long_text(self):
        package = dict(source_catalog=[], timestamped_transcript=[])
        self.assertIn('长文本提醒', capacity_summary(package, [{'content': 'a' * 100001}]))

    def test_exact_bytes_match_transport_encoding(self):
        config = ProviderConfig('http://localhost:8123', 'local', '')
        messages = [{'role': 'user', 'content': '带时间戳的台词🙂'}]
        self.assertEqual(request_size_bytes(config, messages), len(_payload(config, messages)))
