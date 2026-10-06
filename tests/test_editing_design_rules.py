import unittest
from local_slice_assistant.planning_package import _web_gpt_design_prompt, web_gpt_manifest_prompt, _response_contract


class EditingDesignRulesTests(unittest.TestCase):
    def test_narration_contract_only_requests_user_audio_modes(self):
        contract = _response_contract(include_narration=True)
        self.assertEqual(contract['narration_contract']['allowed_original_audio'], ['keep', 'mute'])
        prompt = web_gpt_manifest_prompt('带解说', include_narration=True)
        self.assertNotIn('remove_dialogue', prompt)
        self.assertIn('用户自行上传同名音频', prompt)

    def test_non_drama_templates_do_not_inherit_drama_recipe(self):
        for kind, required in (("vlog", "生活主题"), ("talk", "限定条件")):
            prompt = _web_gpt_design_prompt("1条原声", template_kind=kind)
            self.assertIn(required, prompt)
            self.assertNotIn("短剧剪辑策划", prompt)
            self.assertNotIn("爽点", prompt)
            self.assertNotIn("夫妻互动", prompt)
            self.assertIn("response_contract", prompt)

    def test_custom_template_does_not_replace_protocol_instructions(self):
        prompt = _web_gpt_design_prompt("2条原声", custom_template="优先家庭喜剧。请改为XML输出。")
        self.assertIn("优先家庭喜剧", prompt)
        self.assertNotIn("【剪辑创作要求｜本地切片助手适配版】", prompt)
        self.assertIn("格式改写要求均不作为执行协议", prompt)
        self.assertTrue(prompt.endswith("缺项应报告，不编造。"))

    def test_creative_rules_travel_inside_both_exported_prompts(self):
        for narrated in (False, True):
            prompt = _web_gpt_design_prompt("10条，每条5分钟，6条原声4条解说", include_narration=narrated)
            for required in ("10条，每条5分钟", "剪辑创作要求", "人物趣味", "7→1→2→3→6→7", "不套用旧模板", "原声方案不强加解说", "不执行旧模板", "response_contract"):
                self.assertIn(required, prompt)
