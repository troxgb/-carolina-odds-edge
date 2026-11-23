import unittest

from ev_engine import calculate_capped_kelly_stakes, calculate_required_kelly_stakes
from scanner_config import CONFIG


def opportunity(**overrides):
    row = {
        "sport": "NBA",
        "event_id": "game-1",
        "fair_probability": 0.6,
        "odds_decimal": 2.2,
        "flags": "",
    }
    row.update(overrides)
    return row


class VerifyStakeTests(unittest.TestCase):
    def test_verify_flagged_row_gets_zero_stake_by_default(self):
        stakes = calculate_capped_kelly_stakes(
            [opportunity(flags="verify")], bankroll=1000, fraction=0.25
        )
        self.assertEqual(stakes, [0.0])
        self.assertEqual(CONFIG.verify_stake_multiplier, 0.0)

    def test_verify_flag_is_matched_as_a_whole_token(self):
        stakes = calculate_capped_kelly_stakes(
            [opportunity(flags="line-adjusted, verify")], bankroll=1000, fraction=0.25
        )
        self.assertEqual(stakes, [0.0])
        stakes = calculate_capped_kelly_stakes(
            [opportunity(flags="line-adjusted")], bankroll=1000, fraction=0.25
        )
        self.assertGreater(stakes[0], 0)

    def test_verify_multiplier_one_restores_the_stake(self):
        stakes = calculate_capped_kelly_stakes(
            [opportunity(flags="verify")],
            bankroll=1000,
            fraction=0.25,
            verify_stake_multiplier=1.0,
        )
        self.assertGreater(stakes[0], 0)

    def test_verify_rows_do_not_consume_the_event_cap(self):
        stakes = calculate_capped_kelly_stakes(
            [opportunity(flags="verify"), opportunity()],
            bankroll=1000,
            fraction=0.25,
            per_bet_cap_fraction=1,
            per_event_cap_fraction=0.01,
        )
        self.assertEqual(stakes[0], 0.0)
        self.assertEqual(stakes[1], 10.0)

    def test_invalid_verify_multiplier_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_capped_kelly_stakes(
                [opportunity()], bankroll=1000, fraction=0.25, verify_stake_multiplier=2
            )


class RequiredStakeTests(unittest.TestCase):
    def test_required_stake_ignores_liquidity_but_displayed_stake_does_not(self):
        row = opportunity(liquidity=8.0)
        required = calculate_required_kelly_stakes(
            [row], bankroll=1000, fraction=0.25,
            per_bet_cap_fraction=1, per_event_cap_fraction=1,
        )[0]
        capped = calculate_capped_kelly_stakes(
            [row], bankroll=1000, fraction=0.25,
            per_bet_cap_fraction=1, per_event_cap_fraction=1,
        )[0]
        self.assertAlmostEqual(required, 66.67, places=2)
        self.assertEqual(capped, 4.0)

    def test_required_stake_respects_max_single_bet(self):
        stakes = calculate_required_kelly_stakes(
            [opportunity()], bankroll=1000, fraction=0.25,
            per_bet_cap_fraction=1, per_event_cap_fraction=1,
            max_single_bet=25.0,
        )
        self.assertEqual(stakes, [25.0])


class LiquidityStakeTests(unittest.TestCase):
    def test_eight_dollar_liquidity_caps_a_thousand_dollar_bankroll_at_four(self):
        stakes = calculate_capped_kelly_stakes(
            [opportunity(liquidity=8.0)],
            bankroll=1000,
            fraction=0.25,
            per_bet_cap_fraction=1,
            per_event_cap_fraction=1,
        )
        self.assertLessEqual(stakes[0], 4.0)
        self.assertEqual(stakes[0], 4.0)

    def test_liquidity_cap_is_not_applied_when_liquidity_is_unreported(self):
        uncapped = calculate_capped_kelly_stakes(
            [opportunity(liquidity=None)],
            bankroll=1000,
            fraction=0.25,
            per_bet_cap_fraction=1,
            per_event_cap_fraction=1,
        )[0]
        self.assertGreater(uncapped, 4.0)

    def test_liquidity_fraction_is_configurable_and_bounded(self):
        stakes = calculate_capped_kelly_stakes(
            [opportunity(liquidity=8.0)],
            bankroll=1000,
            fraction=0.25,
            per_bet_cap_fraction=1,
            per_event_cap_fraction=1,
            liquidity_stake_fraction=0.25,
        )
        self.assertEqual(stakes[0], 2.0)
        with self.assertRaises(ValueError):
            calculate_capped_kelly_stakes(
                [opportunity()], bankroll=1000, fraction=0.25, liquidity_stake_fraction=-1
            )

    def test_non_numeric_liquidity_is_ignored(self):
        for liquidity in ("n/a", True, float("nan")):
            with self.subTest(liquidity=liquidity):
                stakes = calculate_capped_kelly_stakes(
                    [opportunity(liquidity=liquidity)],
                    bankroll=1000,
                    fraction=0.25,
                    per_bet_cap_fraction=1,
                    per_event_cap_fraction=1,
                )
                self.assertGreater(stakes[0], 4.0)

    def test_default_minimum_liquidity_is_twenty_five_dollars(self):
        self.assertEqual(CONFIG.min_liquidity_dollars, 25.0)

    def test_player_props_never_receive_a_kelly_stake(self):
        stakes = calculate_capped_kelly_stakes(
            [
                opportunity(
                    is_player_prop=True,
                    market_key="player_points:24.5",
                ),
                opportunity(event_id="game-2"),
            ],
            bankroll=1000,
            fraction=0.25,
            per_bet_cap_fraction=1,
            per_event_cap_fraction=1,
        )
        self.assertEqual(stakes[0], 0.0)
        self.assertGreater(stakes[1], 0.0)


if __name__ == "__main__":
    unittest.main()
