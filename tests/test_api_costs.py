import unittest

from local_slice_assistant.api_costs import estimate_cost, estimate_input_tokens


class ApiCostEstimateTests(unittest.TestCase):
    def test_uses_rough_documented_character_ratios(self):
        self.assertEqual(estimate_input_tokens("中"), 1)
        self.assertEqual(estimate_input_tokens("a"), 1)
        self.assertGreater(estimate_input_tokens("中文" * 100), 0)

    def test_flash_price_calculation_matches_documented_example(self):
        idle, peak = estimate_cost(1_000_000, 1_000_000)
        self.assertAlmostEqual(idle, 5.0)
        self.assertAlmostEqual(peak, 10.0)

    def test_pro_rate_is_higher_than_flash(self):
        flash = estimate_cost(100_000, 10_000, "deepseek-flash")
        pro = estimate_cost(100_000, 10_000, "deepseek-v4-pro")
        self.assertGreater(pro[0], flash[0])
        self.assertGreater(pro[1], flash[1])


if __name__ == "__main__":
    unittest.main()
