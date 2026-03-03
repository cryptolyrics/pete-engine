import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "projects" / "pete-dfs" / "scripts" / "pete-nba-pipeline.py"
FIXTURES = ROOT / "projects" / "pete-dfs" / "fixtures"


def load_module():
    spec = importlib.util.spec_from_file_location("pete_nba_pipeline", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PetePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_decimal_to_aus_conversion(self):
        self.assertEqual(self.module.decimal_to_aus(2.25), "+125")
        self.assertEqual(self.module.decimal_to_aus(1.50), "-200")

    def test_build_parlay_disabled_without_quant_enable(self):
        odds_data = json.loads((FIXTURES / "sample_odds.json").read_text())
        games_data = json.loads((FIXTURES / "sample_games.json").read_text())

        rules = {
            "enabled": False,
            "max_single_bet_decimal_odds": 3.0,
            "max_parlay_legs": 3,
        }
        parlay = self.module.build_parlay(games_data, odds_data, rules)
        self.assertEqual(parlay["legs"], [])
        self.assertIn("NO_PARLAY", parlay["edge_notes"])

    def test_get_bet_pick_returns_no_bet_by_default(self):
        odds_data = json.loads((FIXTURES / "sample_odds.json").read_text())
        games_data = json.loads((FIXTURES / "sample_games.json").read_text())

        rules = {
            "enabled": False,
            "min_edge_pct": 0.03,
            "min_model_prob": 0.52,
            "max_single_bet_decimal_odds": 3.0,
        }
        bet = self.module.get_bet_pick(games_data, odds_data, rules)
        self.assertEqual(bet["pick"], "NO_BET")
        self.assertIn("reason", bet)


if __name__ == "__main__":
    unittest.main()
