import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from scanner_config import CONFIG
from ev_engine import calculate_capped_kelly_stakes
from scanner_logic import (
    devig_two_way,
    estimate_line_adjusted_probability,
    executable_odds_for_stake,
    expected_value_dollars,
    expected_value_pct,
    _standard_deviation_for_sport,
    find_arbitrage_opportunities,
    scan_pinnacle_ev,
    evaluate_arbitrage_opportunity,
    format_arbitrage_evaluation,
    market_integrity,
)


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FRESH_QUOTE_TIME = "2026-01-01T11:59:30Z"
KICKOFF = "2026-01-01T13:00:00Z"


def quote(
    bookmaker,
    selection,
    decimal_odds,
    *,
    market_key="h2h",
    period="",
    point=None,
    team=None,
    expected=None,
    quote_time=FRESH_QUOTE_TIME,
    kickoff=KICKOFF,
):
    is_pinnacle = bookmaker == "Pinnacle"
    return {
        "sport": "NBA",
        "event_id": "game-1",
        "matchup": "Home vs Away",
        "kickoff": kickoff,
        "market": market_key,
        "market_key": market_key,
        "period": period,
        "team": team,
        "point": point,
        "selection": selection,
        "expected_outcomes": expected or ["Home", "Away"],
        "sportsbook": bookmaker,
        "bookmaker_key": "pinnacle" if is_pinnacle else bookmaker.casefold(),
        "odds_decimal": decimal_odds,
        "odds_american": 100 if decimal_odds >= 2 else -110,
        "last_seen_at": quote_time,
    }


def two_way_quotes(
    *,
    market_key="h2h",
    period="",
    point_a=None,
    point_b=None,
    team=None,
    pin_a=1.9090909091,
    pin_b=1.9090909091,
    book_a=2.1,
    book_b=1.8,
    expected=None,
    quote_time=FRESH_QUOTE_TIME,
    kickoff=KICKOFF,
):
    outcomes = expected or ["Home", "Away"]
    return [
        quote(
            "Pinnacle",
            outcomes[0],
            pin_a,
            market_key=market_key,
            period=period,
            point=point_a,
            team=team,
            expected=outcomes,
            quote_time=quote_time,
            kickoff=kickoff,
        ),
        quote(
            "Pinnacle",
            outcomes[1],
            pin_b,
            market_key=market_key,
            period=period,
            point=point_b,
            team=team,
            expected=outcomes,
            quote_time=quote_time,
            kickoff=kickoff,
        ),
        quote(
            "DraftKings",
            outcomes[0],
            book_a,
            market_key=market_key,
            period=period,
            point=point_a,
            team=team,
            expected=outcomes,
            quote_time=quote_time,
            kickoff=kickoff,
        ),
        quote(
            "FanDuel",
            outcomes[1],
            book_b,
            market_key=market_key,
            period=period,
            point=point_b,
            team=team,
            expected=outcomes,
            quote_time=quote_time,
            kickoff=kickoff,
        ),
    ]


def pinnacle_spread_line(point, fair_probability, *, sport="NBA", event_id="game-1"):
    home_decimal = 1 / fair_probability
    away_decimal = 1 / (1 - fair_probability)
    home = quote(
        "Pinnacle",
        "Home",
        home_decimal,
        market_key=f"spreads:{point}",
        point=point,
        expected=["Home", "Away"],
    )
    away = quote(
        "Pinnacle",
        "Away",
        away_decimal,
        market_key=f"spreads:{-point}",
        point=-point,
        expected=["Home", "Away"],
    )
    home["sport"] = sport
    away["sport"] = sport
    home["event_id"] = event_id
    away["event_id"] = event_id
    return [home, away]


def pinnacle_total_line(point, fair_over, *, sport="NBA", event_id="game-1"):
    over = quote(
        "Pinnacle",
        "Over",
        1 / fair_over,
        market_key=f"totals:{point}",
        point=point,
        expected=["Over", "Under"],
    )
    under = quote(
        "Pinnacle",
        "Under",
        1 / (1 - fair_over),
        market_key=f"totals:{point}",
        point=point,
        expected=["Over", "Under"],
    )
    over["sport"] = sport
    under["sport"] = sport
    over["event_id"] = event_id
    under["event_id"] = event_id
    return [over, under]


class PinnacleFairProbabilityTests(unittest.TestCase):
    def test_minus_110_prices_devig_to_fifty_fifty(self):
        fair_a, fair_b = devig_two_way(1 + 100 / 110, 1 + 100 / 110)
        self.assertAlmostEqual(fair_a, 0.5, places=12)
        self.assertAlmostEqual(fair_b, 0.5, places=12)

    def test_power_fair_probabilities_sum_to_one(self):
        for prices in ((1.91, 1.91), (2.5, 1.6), (11.0, 1.06)):
            fair_a, fair_b = devig_two_way(*prices, method="power")
            self.assertAlmostEqual(fair_a + fair_b, 1.0, delta=1e-9)
        shin_fair = devig_two_way(2.5, 1.6, method="shin")
        self.assertAlmostEqual(sum(shin_fair), 1.0, delta=1e-9)

    def test_ev_formula(self):
        self.assertAlmostEqual(expected_value_pct(2.1, 0.5), 5.0)
        self.assertAlmostEqual(expected_value_dollars(2.1, 0.5, 100), 5.0)

    def test_power_devig_reduces_lopsided_longshot_probability(self):
        power_longshot = devig_two_way(11.0, 1.06, "power")[0]
        proportional_longshot = devig_two_way(11.0, 1.06, "multiplicative")[0]
        self.assertLess(power_longshot, proportional_longshot)

    def test_spread_with_two_identical_signed_points_is_rejected(self):
        quotes = two_way_quotes(
            market_key="spreads:23.5",
            point_a=-23.5,
            point_b=-23.5,
            expected=["Home", "Away"],
        )
        result = scan_pinnacle_ev(quotes, now=NOW)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["diagnostics"]["dropped"]["line_mismatch"], 2)

    def test_one_sided_pinnacle_market_is_counted_and_rejected(self):
        quotes = two_way_quotes()
        quotes.remove(next(item for item in quotes if item["bookmaker_key"] == "pinnacle" and item["selection"] == "Away"))
        result = scan_pinnacle_ev(quotes, now=NOW)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(
            result["diagnostics"]["dropped"]["one_sided_pinnacle_market"], 2
        )

    def test_missing_pinnacle_reference_does_not_fall_back_to_soft_books(self):
        only_books = [
            quote("DraftKings", "Home", 10.0),
            quote("FanDuel", "Away", 1.05),
        ]
        result = scan_pinnacle_ev(only_books, now=NOW)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(
            result["diagnostics"]["dropped"]["no_pinnacle_reference"], 2
        )

    def test_bookmaker_three_way_price_does_not_match_pinnacle_two_way_market(self):
        pinnacle_pair = two_way_quotes()[:2]
        three_way_price = quote(
            "DraftKings",
            "Home",
            3.0,
            expected=["Home", "Away", "Draw"],
        )
        result = scan_pinnacle_ev(
            pinnacle_pair + [three_way_price], now=NOW
        )
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["diagnostics"]["dropped"]["line_mismatch"], 1)

    def test_period_and_team_scope_do_not_match_full_game_total(self):
        pin_full_game = two_way_quotes(
            market_key="totals:220.5",
            point_a=220.5,
            point_b=220.5,
            expected=["Over", "Under"],
            book_a=2.1,
            book_b=1.8,
        )[:2]
        first_half_book = quote(
            "DraftKings",
            "Over",
            2.1,
            market_key="totals:220.5",
            period="first_half",
            point=220.5,
            expected=["Over", "Under"],
        )
        period_result = scan_pinnacle_ev(pin_full_game + [first_half_book], now=NOW)
        self.assertEqual(
            period_result["diagnostics"]["dropped"]["period_mismatch"], 1
        )
        self.assertEqual(period_result["opportunities"], [])

        team_scope_book = quote(
            "DraftKings",
            "Over",
            2.1,
            market_key="totals:220.5:team:home",
            point=220.5,
            team="Home",
            expected=["Over", "Under"],
        )
        team_result = scan_pinnacle_ev(pin_full_game + [team_scope_book], now=NOW)
        self.assertEqual(
            team_result["diagnostics"]["dropped"]["team_scope_mismatch"], 1
        )
        self.assertEqual(team_result["opportunities"], [])

    def test_pinnacle_is_reference_only_and_large_edges_are_flagged(self):
        result = scan_pinnacle_ev(
            two_way_quotes(book_a=2.4, book_b=1.8), now=NOW
        )
        self.assertEqual(len(result["opportunities"]), 1)
        row = result["opportunities"][0]
        self.assertEqual(row["sportsbook"], "DraftKings")
        self.assertEqual(row["reference"], "Pinnacle")
        self.assertEqual(row["flags"], "verify")
        self.assertAlmostEqual(row["fair_probability"], 0.5, places=6)
        self.assertEqual(row["pinnacle_american_this_side"], -110)
        self.assertEqual(row["pinnacle_american_other_side"], -110)

    def test_missing_quote_timestamps_do_not_cause_stale_drops(self):
        result = scan_pinnacle_ev(
            two_way_quotes(book_a=2.4, quote_time=None), now=NOW
        )
        self.assertTrue(result["opportunities"])
        self.assertEqual(result["diagnostics"]["dropped"]["stale_quote"], 0)

    def test_started_event_is_dropped(self):
        result = scan_pinnacle_ev(
            two_way_quotes(kickoff="2026-01-01T11:00:00Z"), now=NOW
        )
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["diagnostics"]["dropped"]["started"], 2)

    def test_same_fair_probability_is_applied_to_each_bettable_book(self):
        reference = two_way_quotes(book_a=2.2, book_b=2.0)[:2]
        draftkings_result = scan_pinnacle_ev(
            reference + [quote("DraftKings", "Home", 2.2)], now=NOW
        )
        betmgm_result = scan_pinnacle_ev(
            reference + [quote("BetMGM", "Home", 2.3)], now=NOW
        )
        self.assertEqual(draftkings_result["opportunities"][0]["sportsbook"], "DraftKings")
        self.assertEqual(betmgm_result["opportunities"][0]["sportsbook"], "BetMGM")
        self.assertEqual(
            draftkings_result["opportunities"][0]["fair_probability"],
            betmgm_result["opportunities"][0]["fair_probability"],
        )

    def test_minimum_liquidity_filters_only_reported_values_below_threshold(self):
        config = replace(CONFIG, min_liquidity_dollars=100.0)
        quotes = two_way_quotes(book_a=2.5)
        pinnacle_quotes = quotes[:2]
        low_liquidity_quote = quotes[2]
        low_liquidity_quote["liquidity"] = 99.99

        filtered = scan_pinnacle_ev(
            pinnacle_quotes + [low_liquidity_quote], config=config, now=NOW
        )
        self.assertEqual(filtered["opportunities"], [])
        self.assertEqual(filtered["diagnostics"]["dropped"]["below_min_liquidity"], 1)

        for liquidity in (None, 100.0):
            with self.subTest(liquidity=liquidity):
                eligible_quote = quote("DraftKings", "Home", 2.5)
                eligible_quote["liquidity"] = liquidity
                result = scan_pinnacle_ev(
                    pinnacle_quotes + [eligible_quote], config=config, now=NOW
                )
                self.assertEqual(len(result["opportunities"]), 1)
                self.assertEqual(
                    result["diagnostics"]["dropped"]["below_min_liquidity"], 0
                )

    def test_alt_line_ladder_keeps_only_the_highest_ev_selection_rung(self):
        quotes = []
        for line, book_odds in ((1.5, 2.5), (2.5, 2.1)):
            quotes.extend(
                two_way_quotes(
                    market_key=f"spreads:{line}",
                    point_a=-line,
                    point_b=line,
                    book_a=book_odds,
                    book_b=1.8,
                )
            )
        result = scan_pinnacle_ev(quotes, now=NOW)
        home_rows = [
            row
            for row in result["opportunities"]
            if row["selection"] == "Home"
        ]
        self.assertEqual(len(home_rows), 1)
        self.assertEqual(home_rows[0]["point"], -1.5)
        self.assertEqual(result["diagnostics"]["dropped"]["alt_line_duplicate"], 1)

    def test_stale_and_high_overround_references_are_dropped(self):
        stale = two_way_quotes(
            quote_time="2026-01-01T11:00:00Z", book_a=2.4
        )
        stale_result = scan_pinnacle_ev(stale, now=NOW)
        self.assertEqual(stale_result["opportunities"], [])
        self.assertEqual(stale_result["diagnostics"]["dropped"]["stale_quote"], 2)

        high_overround = two_way_quotes(
            pin_a=1.5, pin_b=1.5, book_a=2.4
        )
        overround_result = scan_pinnacle_ev(high_overround, now=NOW)
        self.assertEqual(overround_result["opportunities"], [])
        self.assertEqual(
            overround_result["diagnostics"]["dropped"]["overround_too_high"], 2
        )

    def test_line_adjustment_is_disabled_by_default(self):
        references = pinnacle_spread_line(-2.5, 0.55)
        venue = quote(
            "DraftKings",
            "Home",
            2.1,
            market_key="spreads:-3.5",
            point=-3.5,
            expected=["Home", "Away"],
        )
        result = scan_pinnacle_ev(references + [venue], now=NOW)
        self.assertFalse(CONFIG.enable_line_adjusted)
        self.assertEqual(result["opportunities"], [])

    def test_exact_pinnacle_line_takes_precedence_when_line_adjustment_is_enabled(self):
        references = (
            pinnacle_spread_line(-2.5, 0.6)
            + pinnacle_spread_line(-4.5, 0.4)
            + pinnacle_spread_line(-3.5, 0.52)
        )
        venue = quote(
            "DraftKings",
            "Home",
            2.4,
            market_key="spreads:-3.5",
            point=-3.5,
            expected=["Home", "Away"],
        )
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        row = result["opportunities"][0]
        self.assertEqual(row["pricing_method"], "exact")
        self.assertAlmostEqual(row["fair_probability"], 0.52)

    def test_line_adjustment_interpolates_between_pinnacle_lines(self):
        references = (
            pinnacle_spread_line(-2.5, 0.6)
            + pinnacle_spread_line(-4.5, 0.4)
        )
        venue = quote(
            "DraftKings",
            "Home",
            2.4,
            market_key="spreads:-3.5",
            point=-3.5,
            expected=["Home", "Away"],
        )
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        row = result["opportunities"][0]
        self.assertAlmostEqual(row["fair_probability"], 0.5)
        self.assertEqual(row["pricing_method"], "line-adjusted")
        self.assertEqual(row["line_adjustment_method"], "interpolation")
        self.assertEqual(row["flags"], "line-adjusted, verify")
        self.assertEqual(row["pinnacle_lines_used"], "-4.5, -2.5")
        self.assertEqual(row["line"], -3.5)
        self.assertEqual(
            row["kelly_fraction"],
            CONFIG.effective_line_adjusted_kelly_fraction,
        )
        stake = calculate_capped_kelly_stakes(
            [row],
            bankroll=10000,
            fraction=0.25,
            per_bet_cap_fraction=1,
            per_event_cap_fraction=1,
            verify_stake_multiplier=1.0,
        )[0]
        self.assertAlmostEqual(
            stake,
            round(
                10000
                * ((0.5 * 2.4 - 1) / (2.4 - 1))
                * CONFIG.effective_line_adjusted_kelly_fraction,
                2,
            ),
        )
        self.assertAlmostEqual(row["ev_pct"], 20.0)

    def test_line_adjustment_uses_distribution_when_no_lines_bracket_target(self):
        references = pinnacle_spread_line(-2.5, 0.5, sport="NFL")
        venue = quote(
            "DraftKings",
            "Home",
            2.4,
            market_key="spreads:-3.5",
            point=-3.5,
            expected=["Home", "Away"],
        )
        venue["sport"] = "NFL"
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        row = result["opportunities"][0]
        self.assertEqual(row["line_adjustment_method"], "distribution")
        self.assertLess(row["fair_probability"], 0.5)

    def test_total_line_interpolation_preserves_over_under_direction(self):
        references = (
            pinnacle_total_line(200.5, 0.6)
            + pinnacle_total_line(202.5, 0.4)
        )
        venue = quote(
            "DraftKings",
            "Over",
            2.4,
            market_key="totals:201.5",
            point=201.5,
            expected=["Over", "Under"],
        )
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        row = result["opportunities"][0]
        self.assertAlmostEqual(row["fair_probability"], 0.5)
        self.assertEqual(row["line_adjustment_method"], "interpolation")

    def test_line_adjustment_does_not_extrapolate_past_max_line_difference(self):
        references = (
            pinnacle_spread_line(-2.5, 0.6)
            + pinnacle_spread_line(-4.5, 0.4)
        )
        venue = quote(
            "DraftKings",
            "Home",
            2.4,
            market_key="spreads:-6.5",
            point=-6.5,
            expected=["Home", "Away"],
        )
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["diagnostics"]["dropped"]["line_mismatch"], 1)

    def test_football_key_number_crossing_has_extra_probability_effect(self):
        crossing = estimate_line_adjusted_probability(
            "NFL",
            "spreads",
            "Home",
            -3.5,
            [{"point": -2.5, "fair_probability": 0.5, "overround": 1.04}],
        )
        ordinary = estimate_line_adjusted_probability(
            "NFL",
            "spreads",
            "Home",
            -5.5,
            [{"point": -4.5, "fair_probability": 0.5, "overround": 1.04}],
        )
        self.assertGreater(
            abs(crossing["fair_probability"] - 0.5),
            abs(ordinary["fair_probability"] - 0.5),
        )

    def test_line_adjustment_never_crosses_period_or_team_scope(self):
        config = replace(CONFIG, enable_line_adjusted=True)
        full_game = two_way_quotes(
            market_key="totals:220.5",
            point_a=220.5,
            point_b=220.5,
            expected=["Over", "Under"],
        )[:2]
        first_half = quote(
            "DraftKings",
            "Over",
            2.1,
            market_key="totals:221.5",
            period="first_half",
            point=221.5,
            expected=["Over", "Under"],
        )
        period_result = scan_pinnacle_ev(
            full_game + [first_half], config=config, now=NOW
        )
        self.assertEqual(period_result["opportunities"], [])
        self.assertEqual(
            period_result["diagnostics"]["dropped"]["period_mismatch"], 1
        )

        team_total = quote(
            "DraftKings",
            "Over",
            2.1,
            market_key="totals:221.5:team:home",
            point=221.5,
            team="Home",
            expected=["Over", "Under"],
        )
        team_result = scan_pinnacle_ev(
            full_game + [team_total], config=config, now=NOW
        )
        self.assertEqual(team_result["opportunities"], [])
        self.assertEqual(
            team_result["diagnostics"]["dropped"]["team_scope_mismatch"], 1
        )

    def test_whole_number_line_estimates_push_and_uses_push_aware_ev(self):
        exact_result = scan_pinnacle_ev(
            two_way_quotes(
                market_key="spreads:-3",
                point_a=-3,
                point_b=3,
                book_a=2.2,
            ),
            now=NOW,
        )
        exact_row = exact_result["opportunities"][0]
        self.assertEqual(exact_row["pricing_method"], "exact")
        self.assertGreater(exact_row["push_probability"], 0)

        references = pinnacle_spread_line(-2.5, 0.55)
        venue = quote(
            "DraftKings",
            "Home",
            2.2,
            market_key="spreads:-3",
            point=-3,
            expected=["Home", "Away"],
        )
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        row = result["opportunities"][0]
        self.assertGreater(row["push_probability"], 0)
        self.assertAlmostEqual(
            row["ev_pct"],
            (
                row["win_probability"] * (row["odds_decimal"] - 1)
                - row["lose_probability"]
            )
            * 100,
        )


class ExistingScannerBehaviorTests(unittest.TestCase):
    def test_arbitrage_preserves_quote_prices_and_liquidity(self):
        quotes = [
            {
                "sport": "NBA",
                "event_id": "game-1",
                "market_key": "h2h",
                "period": "",
                "matchup": "Sample",
                "market": "Moneyline",
                "selection": "Home",
                "expected_outcomes": ["Home", "Away"],
                "sportsbook": "A",
                "odds_american": 150,
                "odds_decimal": 2.5,
                "liquidity": 400,
                "liquidity_updated_at": "2026-11-13T10:00:00Z",
                "last_seen_at": "2026-11-13T10:00:00Z",
            },
            {
                "sport": "NBA",
                "event_id": "game-1",
                "market_key": "h2h",
                "period": "",
                "matchup": "Sample",
                "market": "Moneyline",
                "selection": "Away",
                "expected_outcomes": ["Home", "Away"],
                "sportsbook": "B",
                "odds_decimal": 1.8,
                "last_seen_at": "2026-11-13T10:00:00Z",
            },
        ]
        opportunities = find_arbitrage_opportunities(
            quotes,
            300,
            rounding_step=1,
            now=datetime(2026, 11, 13, 10, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(opportunities), 1)
        arb = opportunities[0]
        self.assertEqual(sum(leg["stake"] for leg in arb["legs"]), 300)
        self.assertGreater(arb["profit"], 0)
        self.assertAlmostEqual(arb["profit"], arb["locked_payout"] - 300)
        first_leg = next(leg for leg in arb["legs"] if leg["sportsbook"] == "A")
        self.assertEqual(first_leg["odds_american"], 150)
        self.assertEqual(first_leg["liquidity"], 400)
        self.assertEqual(
            first_leg["liquidity_updated_at"], "2026-11-13T10:00:00Z"
        )

    def test_arbitrage_excludes_stale_quotes_and_uses_market_update_fallback(self):
        stale_time = (NOW - timedelta(seconds=CONFIG.max_quote_age_seconds + 1)).isoformat()
        stale_quotes = [
            quote("A", "Home", 2.5, quote_time=stale_time),
            quote("B", "Away", 2.0, quote_time=stale_time),
        ]
        self.assertEqual(
            find_arbitrage_opportunities(stale_quotes, 100, now=NOW),
            [],
        )

        fresh_quotes = [
            {
                **quote("A", "Home", 2.5, quote_time=None),
                "last_change_at": stale_time,
                "last_update": NOW.isoformat(),
            },
            quote("B", "Away", 2.0, quote_time=NOW.isoformat()),
        ]
        self.assertEqual(
            len(find_arbitrage_opportunities(fresh_quotes, 200, now=NOW)),
            1,
        )
        fresh_quotes[1].pop("last_seen_at")
        self.assertEqual(
            find_arbitrage_opportunities(fresh_quotes, 100, now=NOW),
            [],
        )

    def test_fractional_kelly_respects_per_bet_and_per_event_caps_to_cents(self):
        opportunities = [
            {
                "sport": "NBA",
                "event_id": "same-event",
                "fair_probability": 0.8,
                "odds_decimal": 2.0,
            }
            for _ in range(4)
        ]
        opportunities.append(
            {
                "sport": "NBA",
                "event_id": "other-event",
                "fair_probability": 0.8,
                "odds_decimal": 2.0,
            }
        )
        stakes = calculate_capped_kelly_stakes(
            opportunities, bankroll=1000, fraction=0.5
        )
        self.assertTrue(all(stake <= 10 for stake in stakes))
        self.assertLessEqual(sum(stakes[:4]), 30)
        self.assertTrue(all(round(stake, 2) == stake for stake in stakes))
        self.assertEqual(stakes[4], 10.0)


class BookFeeTests(unittest.TestCase):
    def _fee_config(self, dk_fee=0.1, fd_fee=0.0):
        return replace(CONFIG, book_fee_pct=(("DraftKings", dk_fee), ("FanDuel", fd_fee)))

    def test_default_fees_leave_ev_and_net_odds_unchanged(self):
        result = scan_pinnacle_ev(two_way_quotes(book_a=2.5), now=NOW)
        row = next(r for r in result["opportunities"] if r["sportsbook"] == "DraftKings")
        self.assertAlmostEqual(row["ev_pct"], 25.0)
        self.assertEqual(row["odds_decimal"], 2.5)
        self.assertAlmostEqual(row["odds_decimal_net"], 2.5)

    def test_configured_fee_reduces_ev_but_keeps_quoted_odds(self):
        result = scan_pinnacle_ev(
            two_way_quotes(book_a=2.5), config=self._fee_config(), now=NOW
        )
        row = next(r for r in result["opportunities"] if r["sportsbook"] == "DraftKings")
        self.assertEqual(row["odds_decimal"], 2.5)
        self.assertAlmostEqual(row["odds_decimal_net"], 2.35)
        self.assertAlmostEqual(row["ev_pct"], 17.5)

    def test_fee_applies_only_to_the_named_bookmaker(self):
        result = scan_pinnacle_ev(
            two_way_quotes(book_a=2.5, book_b=2.5), config=self._fee_config(), now=NOW
        )
        by_book = {r["sportsbook"]: r for r in result["opportunities"]}
        self.assertAlmostEqual(by_book["DraftKings"]["ev_pct"], 17.5)
        self.assertAlmostEqual(by_book["FanDuel"]["ev_pct"], 25.0)
        self.assertAlmostEqual(by_book["FanDuel"]["odds_decimal_net"], 2.5)

    def test_fee_reduces_kelly_stake_through_net_odds(self):
        plain = scan_pinnacle_ev(two_way_quotes(book_a=2.5), now=NOW)
        feed = scan_pinnacle_ev(
            two_way_quotes(book_a=2.5), config=self._fee_config(), now=NOW
        )
        plain_row = next(r for r in plain["opportunities"] if r["sportsbook"] == "DraftKings")
        fee_row = next(r for r in feed["opportunities"] if r["sportsbook"] == "DraftKings")
        plain_stake, fee_stake = calculate_capped_kelly_stakes(
            [plain_row, fee_row], bankroll=100000, fraction=0.25,
            per_bet_cap_fraction=1, per_event_cap_fraction=1,
            verify_stake_multiplier=1.0,
        )
        self.assertGreater(plain_stake, 0)
        self.assertLess(fee_stake, plain_stake)
        self.assertAlmostEqual(
            fee_stake,
            round(100000 * ((0.5 * 2.35 - 1) / (2.35 - 1)) * 0.25, 2),
        )

    def test_push_aware_ev_uses_net_odds(self):
        result = scan_pinnacle_ev(
            two_way_quotes(market_key="spreads:-3", point_a=-3, point_b=3, book_a=2.2),
            config=self._fee_config(),
            now=NOW,
        )
        row = next(r for r in result["opportunities"] if r["sportsbook"] == "DraftKings")
        self.assertGreater(row["push_probability"], 0)
        self.assertAlmostEqual(
            row["ev_pct"],
            (
                row["win_probability"] * (row["odds_decimal_net"] - 1)
                - row["lose_probability"]
            )
            * 100,
        )
        self.assertLess(row["odds_decimal_net"], row["odds_decimal"])

    def test_invalid_fee_fails_loudly_instead_of_dropping_rows(self):
        for bad_fee in (1.0, -0.1, float("nan")):
            with self.subTest(fee=bad_fee):
                with self.assertRaises(ValueError):
                    scan_pinnacle_ev(
                        two_way_quotes(book_a=2.5),
                        config=self._fee_config(dk_fee=bad_fee),
                        now=NOW,
                    )


class WholeLineBracketAndSdTests(unittest.TestCase):
    def ref(self, point, probability):
        return {"point": point, "fair_probability": probability, "overround": 1.0}

    def test_spread_bracket_is_exact_for_home_side(self):
        refs = [self.ref(-3.5, 0.55), self.ref(-2.5, 0.60)]
        estimate = estimate_line_adjusted_probability("NBA", "spreads", "Home", -3.0, refs, CONFIG)
        self.assertEqual(estimate["method"], "bracket_exact")
        self.assertAlmostEqual(estimate["win_probability"], 0.55)
        self.assertAlmostEqual(estimate["lose_probability"], 0.40)
        self.assertAlmostEqual(estimate["push_probability"], 0.05)
        self.assertAlmostEqual(estimate["fair_probability"], 0.55 / 0.95)

    def test_spread_bracket_is_exact_for_away_side(self):
        refs = [self.ref(3.5, 0.45), self.ref(2.5, 0.40)]
        estimate = estimate_line_adjusted_probability("NBA", "spreads", "Away", 3.0, refs, CONFIG)
        self.assertEqual(estimate["method"], "bracket_exact")
        self.assertAlmostEqual(estimate["win_probability"], 0.40)
        self.assertAlmostEqual(estimate["lose_probability"], 0.55)
        self.assertAlmostEqual(estimate["push_probability"], 0.05)

    def test_total_over_bracket_uses_negated_coordinates(self):
        refs = [self.ref(220.5, 0.45), self.ref(219.5, 0.55)]
        estimate = estimate_line_adjusted_probability("NBA", "totals", "Over", 220.0, refs, CONFIG)
        self.assertEqual(estimate["method"], "bracket_exact")
        self.assertAlmostEqual(estimate["win_probability"], 0.45)
        self.assertAlmostEqual(estimate["lose_probability"], 0.45)
        self.assertAlmostEqual(estimate["push_probability"], 0.10)

    def test_total_under_bracket_uses_direct_coordinates(self):
        refs = [self.ref(219.5, 0.45), self.ref(220.5, 0.55)]
        estimate = estimate_line_adjusted_probability("NBA", "totals", "Under", 220.0, refs, CONFIG)
        self.assertEqual(estimate["method"], "bracket_exact")
        self.assertAlmostEqual(estimate["win_probability"], 0.45)
        self.assertAlmostEqual(estimate["push_probability"], 0.10)

    def test_bracket_is_skipped_when_loose_probability_is_lower(self):
        refs = [self.ref(-3.5, 0.60), self.ref(-2.5, 0.55)]
        estimate = estimate_line_adjusted_probability("NBA", "spreads", "Home", -3.0, refs, CONFIG)
        self.assertTrue(estimate is None or estimate["method"] != "bracket_exact")

    def test_standard_deviation_is_keyed_by_sport_and_market(self):
        self.assertEqual(_standard_deviation_for_sport("NBA", "totals", CONFIG), 15.0)
        self.assertIsNone(_standard_deviation_for_sport("Golf", "spreads", CONFIG))
        self.assertIsNone(_standard_deviation_for_sport("NBA", "moneyline", CONFIG))

    def test_missing_sd_disables_distribution_path_for_that_market(self):
        config = replace(
            CONFIG,
            line_adjusted_stddev_by_sport=(
                (("NBA", "totals"), 15.0),
            ),
        )
        refs = [self.ref(-2.5, 0.60)]
        self.assertIsNone(
            estimate_line_adjusted_probability("NBA", "spreads", "Home", -4.0, refs, config)
        )
        self.assertIsNotNone(
            estimate_line_adjusted_probability("NBA", "totals", "Over", 221.0, [self.ref(220.0, 0.6)], config)
        )


class ReferenceHygieneTests(unittest.TestCase):
    def test_conflicting_prices_at_same_timestamp_drop_that_pinnacle_selection(self):
        quotes = two_way_quotes(book_a=2.1, book_b=1.8)
        duplicate = quote("Pinnacle", "Home", 1.7, expected=["Home", "Away"])
        duplicate["quote_time"] = None
        duplicate["last_seen_at"] = FRESH_QUOTE_TIME
        result = scan_pinnacle_ev(quotes + [duplicate], now=NOW)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["diagnostics"]["dropped"]["ambiguous_pinnacle_quote"], 2)

    def test_duplicate_prices_at_different_timestamps_are_not_ambiguous(self):
        quotes = two_way_quotes(book_a=2.5)
        earlier = quote("Pinnacle", "Home", 1.7, expected=["Home", "Away"])
        earlier["last_seen_at"] = "2026-01-01T11:50:00Z"
        result = scan_pinnacle_ev([earlier] + quotes, now=NOW)
        self.assertEqual(result["diagnostics"]["dropped"]["ambiguous_pinnacle_quote"], 0)
        self.assertEqual(len(result["opportunities"]), 1)

    def test_untimestamped_quotes_used_are_counted_in_diagnostics(self):
        quotes = two_way_quotes(book_a=2.5, book_b=2.5, quote_time=None)
        result = scan_pinnacle_ev(quotes, now=NOW)
        self.assertEqual(len(result["opportunities"]), 2)
        self.assertEqual(result["diagnostics"]["untimestamped_quotes_used"], 4)

    def test_timestamped_quotes_are_not_counted_as_untimestamped(self):
        result = scan_pinnacle_ev(two_way_quotes(book_a=2.5, book_b=2.5), now=NOW)
        self.assertEqual(result["diagnostics"]["untimestamped_quotes_used"], 0)


def prop_quote(
    bookmaker,
    selection,
    decimal_odds,
    *,
    player_id="player-1",
    player_name="Player One",
    point=24.5,
    market_key="player_points",
    quote_time=FRESH_QUOTE_TIME,
):
    item = quote(
        bookmaker,
        selection,
        decimal_odds,
        market_key=market_key,
        point=point,
        expected=["Over", "Under"],
        quote_time=quote_time,
    )
    item.update(
        {
            "is_player_prop": True,
            "player_id": player_id,
            "player_name": player_name,
        }
    )
    return item


class PlayerPropAndArbitrageSafetyTests(unittest.TestCase):
    def test_player_props_never_receive_an_ev_evaluation(self):
        quotes = [
            prop_quote("Pinnacle", "Over", 1.91),
            prop_quote("Pinnacle", "Under", 1.91),
            prop_quote("DraftKings", "Over", 2.5),
            prop_quote("FanDuel", "Under", 1.80),
        ]
        result = scan_pinnacle_ev(quotes, now=NOW)

        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["pinnacle_snapshots"], [])
        self.assertEqual(
            result["diagnostics"]["dropped"]["player_prop_excluded"], 4
        )
        self.assertFalse(
            any(
                str(row.get("market_key", "")).startswith("player_")
                for row in result["opportunities"]
            )
        )

    def test_pinnacle_is_never_used_as_an_arbitrage_leg(self):
        # Pinnacle 2.5 + DraftKings 2.0 would be a 10.9% arbitrage if the
        # sharp book were allowed to be a leg. It is reference-only.
        quotes = [
            quote("Pinnacle", "Home", 2.5),
            quote("DraftKings", "Away", 2.0),
        ]
        self.assertEqual(
            find_arbitrage_opportunities(quotes, 1000, now=NOW),
            [],
        )

    def test_same_signed_spread_points_never_form_an_arbitrage(self):
        # Both venues quote -13.5 for their own team. Those are two separate
        # "team covers -13.5" bets, not opposite sides of one line, so the
        # apparently huge edge is a phantom.
        quotes = [
            quote(
                "DraftKings",
                "Home",
                21.0,
                market_key="spreads:13.5",
                point=-13.5,
            ),
            quote(
                "FanDuel",
                "Away",
                8.33,
                market_key="spreads:13.5",
                point=-13.5,
            ),
        ]
        self.assertEqual(
            find_arbitrage_opportunities(quotes, 500, rounding_step=1, now=NOW),
            [],
        )

    def test_opposite_signed_spread_points_form_an_arbitrage(self):
        quotes = [
            quote(
                "DraftKings",
                "Away",
                2.2,
                market_key="spreads:4.5",
                point=-4.5,
            ),
            quote(
                "FanDuel",
                "Home",
                2.05,
                market_key="spreads:4.5",
                point=4.5,
            ),
        ]
        opportunities = find_arbitrage_opportunities(
            quotes, 500, rounding_step=1, now=NOW
        )
        self.assertEqual(len(opportunities), 1)
        arb = opportunities[0]
        self.assertEqual(
            {float(leg["line"]) for leg in arb["legs"]},
            {-4.5, 4.5},
        )
        self.assertAlmostEqual(
            sum(float(leg["line"]) for leg in arb["legs"]), 0.0
        )

    def test_three_way_moneyline_never_forms_a_two_leg_arbitrage(self):
        # One venue reports a two-way market while another reports a three-way
        # market. The draw still exists, so Home + Away does not cover it even
        # when the draw quote is absent from this snapshot.
        quotes = [
            quote(
                "DraftKings",
                "Home",
                2.2,
                expected=["Home", "Away"],
            ),
            quote(
                "FanDuel",
                "Away",
                2.2,
                expected=["Home", "Away", "Draw"],
            ),
        ]
        self.assertEqual(
            find_arbitrage_opportunities(quotes, 500, rounding_step=1, now=NOW),
            [],
        )

    def test_totals_require_over_and_under_at_the_same_line(self):
        over = quote(
            "DraftKings",
            "Over",
            2.2,
            market_key="totals:44.5",
            point=44.5,
            expected=["Over", "Under"],
        )
        under = quote(
            "FanDuel",
            "Under",
            2.2,
            market_key="totals:44.5",
            point=44.5,
            expected=["Over", "Under"],
        )
        opportunities = find_arbitrage_opportunities(
            [over, under], 500, rounding_step=1, now=NOW
        )
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(
            {leg["selection"] for leg in opportunities[0]["legs"]},
            {"Over", "Under"},
        )

        # Two non-opposite selections at the same totals line are not a market,
        # even when the feed omits expected_outcomes entirely.
        unlabeled_over = {
            key: value
            for key, value in over.items()
            if key != "expected_outcomes"
        }
        wrong_selection = quote(
            "FanDuel",
            "Home",
            2.2,
            market_key="totals:44.5",
            point=44.5,
        )
        wrong_selection.pop("expected_outcomes", None)
        self.assertEqual(
            find_arbitrage_opportunities(
                [unlabeled_over, wrong_selection],
                500,
                rounding_step=1,
                now=NOW,
            ),
            [],
        )

    def test_player_prop_arbitrage_requires_player_identity(self):
        # Without an ID or name both quotes share an empty player scope and
        # could otherwise be paired as if they were the same player.
        over = prop_quote(
            "DraftKings", "Over", 2.2, player_id=None, player_name=None
        )
        under = prop_quote(
            "FanDuel", "Under", 2.2, player_id=None, player_name=None
        )
        self.assertEqual(
            find_arbitrage_opportunities(
                [over, under], 500, rounding_step=1, now=NOW
            ),
            [],
        )

    def test_arbitrage_requires_at_least_ten_profit_at_the_stake(self):
        # 2.5/1.7 at a $100 stake guarantees about $1.20, below the $20 floor.
        low_profit = [
            quote("DraftKings", "Home", 2.5, expected=["Home", "Away"]),
            quote("FanDuel", "Away", 1.7, expected=["Home", "Away"]),
        ]
        self.assertEqual(
            find_arbitrage_opportunities(
                low_profit, 100, rounding_step=1, now=NOW
            ),
            [],
        )

        # 2.2/2.2 at $1,000 guarantees about $100, comfortably above the floor.
        high_profit = [
            quote("DraftKings", "Home", 2.2, expected=["Home", "Away"]),
            quote("FanDuel", "Away", 2.2, expected=["Home", "Away"]),
        ]
        opportunities = find_arbitrage_opportunities(
            high_profit, 1000, rounding_step=1, now=NOW
        )
        self.assertEqual(len(opportunities), 1)
        arb = opportunities[0]
        self.assertEqual(arb["total_stake"], 300)
        self.assertGreater(arb["profit"], 10.0)
        self.assertEqual(arb["min_guaranteed_profit"], 10.0)
        self.assertGreaterEqual(
            min(leg["payout"] for leg in arb["legs"]), arb["total_stake"]
        )
        self.assertEqual(
            round(min(leg["payout"] for leg in arb["legs"]) - arb["total_stake"], 2),
            arb["profit"],
        )

    def test_player_prop_arbitrage_matches_over_and_under_at_same_line(self):
        quotes = [
            prop_quote("DraftKings", "Over", 2.2),
            prop_quote("FanDuel", "Under", 2.2),
        ]
        opportunities = find_arbitrage_opportunities(
            quotes, 1000, rounding_step=1, now=NOW
        )
        self.assertEqual(len(opportunities), 1)
        arb = opportunities[0]
        self.assertTrue(arb["is_player_prop"])
        self.assertEqual(arb["player_name"], "Player One")
        self.assertEqual({leg["selection"] for leg in arb["legs"]}, {"Over", "Under"})
        self.assertEqual({float(leg["line"]) for leg in arb["legs"]}, {24.5})
        self.assertGreaterEqual(arb["profit"], 10.0)

    def test_player_prop_arbitrage_matches_when_one_venue_omits_player_id(self):
        over = prop_quote(
            "DraftKings", "Over", 2.2, player_id="player-1", player_name="Player One"
        )
        under = prop_quote(
            "FanDuel", "Under", 2.2, player_id=None, player_name="player one"
        )
        opportunities = find_arbitrage_opportunities(
            [over, under], 1000, rounding_step=1, now=NOW
        )
        self.assertEqual(len(opportunities), 1)
        self.assertTrue(opportunities[0]["is_player_prop"])
        self.assertEqual(opportunities[0]["player_id"], "player-1")

    def test_player_prop_arbitrage_requires_identical_line_and_two_venues(self):
        different_points = [
            prop_quote("DraftKings", "Over", 2.2, point=24.5),
            prop_quote("FanDuel", "Under", 2.2, point=25.5),
        ]
        self.assertEqual(
            find_arbitrage_opportunities(different_points, 1000, now=NOW), []
        )

        same_venue = [
            prop_quote("DraftKings", "Over", 2.2),
            prop_quote("DraftKings", "Under", 2.2),
        ]
        self.assertEqual(
            find_arbitrage_opportunities(same_venue, 1000, now=NOW), []
        )

    def test_milestone_only_prop_outcomes_are_ignored(self):
        quotes = [
            prop_quote("DraftKings", "30+", 2.2),
            prop_quote("FanDuel", "Under", 2.2),
        ]
        self.assertEqual(
            find_arbitrage_opportunities(quotes, 1000, now=NOW), []
        )


class ArbitrageEvaluationTests(unittest.TestCase):
    def _candidate(self, **overrides):
        candidate = {
            "sport": "NFL",
            "matchup": "Denver Broncos vs Las Vegas Raiders",
            "market": "Spread",
            "constraints": {
                "min_depth_dollars": 150,
                "max_stake_per_bet": 300,
                "bankroll_cap": 1000,
                "min_profit_dollars": 20,
                "min_roi_pct": 1.5,
            },
            "leg_1": {
                "venue": "FanDuel",
                "type": "sportsbook",
                "selection": "Raiders +13.5",
                "decimal_odds": 2.05,
                "max_allowed_stake": 500,
            },
            "leg_2": {
                "venue": "Novig",
                "type": "exchange",
                "selection": "Broncos -13.5",
                "exchange_fee_pct": 0.0,
                "order_book_asks_or_implied": [
                    {"decimal_odds": 2.08, "available_stake": 120},
                    {"decimal_odds": 2.02, "available_stake": 250},
                    {"decimal_odds": 1.95, "available_stake": 500},
                ],
            },
        }
        for key, value in overrides.items():
            if key == "constraints":
                candidate["constraints"].update(value)
            elif key == "leg_1":
                candidate["leg_1"].update(value)
            elif key == "leg_2":
                candidate["leg_2"].update(value)
            else:
                candidate[key] = value
        return candidate

    def test_market_integrity_requires_opposite_spreads(self):
        ok, _ = market_integrity(self._candidate())
        self.assertTrue(ok)
        bad = self._candidate()
        bad["leg_2"]["selection"] = "Broncos +13.5"
        ok, msg = market_integrity(bad)
        self.assertFalse(ok)
        self.assertIn("not opposite", msg)

    def test_depth_below_minimum_is_rejected(self):
        candidate = self._candidate()
        candidate["leg_2"]["order_book_asks_or_implied"] = [
            {"decimal_odds": 2.08, "available_stake": 100}
        ]
        result = evaluate_arbitrage_opportunity(candidate)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("depth", result["binding_bottleneck"].lower())

    def test_candidate_from_prompt_is_rejected_for_profit(self):
        result = evaluate_arbitrage_opportunity(self._candidate())
        self.assertEqual(result["status"], "REJECTED")
        rec = result["recommended_stakes_payouts"]
        self.assertEqual(rec["net_guaranteed_profit"], 0.0)
        self.assertGreater(
            result["effective_odds_fees"]["depth_at_or_above_break_even"], 150
        )

    def test_instant_take_when_thresholds_met(self):
        candidate = self._candidate()
        candidate["constraints"]["min_profit_dollars"] = 5
        candidate["constraints"]["min_roi_pct"] = 1.0
        result = evaluate_arbitrage_opportunity(candidate)
        self.assertEqual(result["status"], "INSTANT_TAKE")
        rec = result["recommended_stakes_payouts"]
        self.assertGreater(rec["net_guaranteed_profit"], 5)
        self.assertGreaterEqual(rec["roi_pct"], 1.0)
        self.assertAlmostEqual(
            rec["locked_payout"],
            rec["exchange_leg_stake"] * result["effective_odds_fees"][
                "volume_weighted_exchange_odds"
            ],
            places=2,
        )

    def test_partial_fill_worst_case_is_reported(self):
        result = evaluate_arbitrage_opportunity(self._candidate())
        import re
        self.assertIn("worst-case loss", result["partial_fill_warning"])
        match = re.search(r"worst-case loss is \$([\d,]+\.\d{2})", result["partial_fill_warning"])
        self.assertIsNotNone(match)
        self.assertGreater(float(match.group(1).replace(",", "")), 0)

    def test_exchange_fee_reduces_effective_odds(self):
        candidate = self._candidate()
        candidate["constraints"]["min_profit_dollars"] = 1
        candidate["constraints"]["min_roi_pct"] = 0.5
        candidate["leg_2"]["exchange_fee_pct"] = 0.02
        result = evaluate_arbitrage_opportunity(candidate)
        self.assertEqual(result["status"], "INSTANT_TAKE")
        self.assertLess(
            result["effective_odds_fees"]["volume_weighted_exchange_odds"],
            2.08,
        )

    def test_format_output_contains_required_sections(self):
        result = evaluate_arbitrage_opportunity(self._candidate())
        text = format_arbitrage_evaluation(result)
        for section in (
            "Status:",
            "Effective Odds & Fees:",
            "Binding Liquidity Bottleneck:",
            "Recommended Stakes & Payouts:",
            "Partial-Fill / Execution Warning:",
        ):
            self.assertIn(section, text)


class ExchangeFeeTests(unittest.TestCase):
    def test_prophetx_fee_lowers_ev_through_net_odds(self):
        quotes = two_way_quotes(book_a=2.5)
        quotes[2]["sportsbook"] = "ProphetX"
        quotes[2]["bookmaker_key"] = "prophetx"
        result = scan_pinnacle_ev(quotes[:3], now=NOW)
        row = result["opportunities"][0]
        # 2% of net winnings: 1 + (2.5 - 1) * 0.98 = 2.47
        self.assertAlmostEqual(row["odds_decimal_net"], 2.47, places=9)
        expected = (0.5 * (2.47 - 1) - 0.5) * 100
        self.assertAlmostEqual(row["ev_pct"], expected, places=9)

    def test_prophetx_arb_uses_net_odds_for_sizing_and_payout(self):
        def quote_for(book, selection, odds, liquidity):
            return {
                "sport": "NBA",
                "event_id": "game-1",
                "market_key": "h2h",
                "period": "",
                "matchup": "Sample",
                "market": "Moneyline",
                "selection": selection,
                "expected_outcomes": ["Home", "Away"],
                "sportsbook": book,
                "odds_american": 150 if odds > 2 else -125,
                "odds_decimal": odds,
                "liquidity": liquidity,
                "last_seen_at": "2026-11-13T10:00:00Z",
            }

        quotes = [
            quote_for("ProphetX", "Home", 2.5, 400),
            quote_for("B", "Away", 1.8, 400),
        ]
        arbs = find_arbitrage_opportunities(
            quotes,
            300,
            rounding_step=1,
            now=datetime(2026, 11, 13, 10, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(arbs), 1)
        exchange_leg = next(
            leg for leg in arbs[0]["legs"] if leg["sportsbook"] == "ProphetX"
        )
        self.assertAlmostEqual(exchange_leg["odds_decimal"], 2.47, places=9)
        self.assertAlmostEqual(exchange_leg["odds_decimal_quoted"], 2.5, places=9)
        self.assertAlmostEqual(
            exchange_leg["payout"],
            round(exchange_leg["stake"] * 2.47, 2),
            places=9,
        )


class ExecutableOddsTests(unittest.TestCase):
    def test_depth_levels_walk_past_top_liquidity(self):
        result = executable_odds_for_stake(
            {
                "odds_decimal": 2.30,
                "liquidity": 50.0,
                "depth": [
                    {"price": 129, "size": 50.0},
                    {"price": 128, "size": 200.0},
                ],
            },
            200.0,
        )
        # 50 @ 2.30 + 50 @ 2.29 + 100 @ 2.28 = 457.50 / 200 = 2.2875
        self.assertAlmostEqual(result["executable_decimal"], 2.2875, places=9)
        self.assertEqual(result["levels_used"], 2)
        self.assertFalse(result["size_unknown"])
        self.assertAlmostEqual(result["slippage_pct"], 0.543478, places=5)

    def test_no_liquidity_leaves_quoted_price_but_flags_unknown_size(self):
        result = executable_odds_for_stake({"odds_decimal": 2.05}, 100.0)
        self.assertEqual(result["executable_decimal"], 2.05)
        self.assertEqual(result["levels_used"], 0)
        self.assertTrue(result["size_unknown"])
        self.assertEqual(result["slippage_pct"], 0.0)

    def test_unmodelled_tail_is_valued_at_worst_published_level(self):
        result = executable_odds_for_stake(
            {
                "odds_decimal": 2.30,
                "liquidity": 10.0,
                "depth": [
                    {"price": 129, "size": 5.0},
                    {"price": 128, "size": 5.0},
                ],
            },
            100.0,
        )
        # 10 @ 2.30 + 5 @ 2.29 + 85 @ 2.28 = 228.25
        self.assertAlmostEqual(result["executable_decimal"], 2.2825, places=9)
        self.assertEqual(result["levels_used"], 2)
        self.assertTrue(result["size_unknown"])

    def test_scan_reports_executable_ev_below_headline_ev(self):
        quotes = two_way_quotes(book_a=2.5)
        quotes[2]["liquidity"] = 40.0
        quotes[2]["depth"] = [
            {"price": 140, "size": 20.0},
            {"price": 130, "size": 20.0},
        ]
        result = scan_pinnacle_ev(quotes, now=NOW)
        row = next(
            item
            for item in result["opportunities"]
            if item["sportsbook"] == "DraftKings"
        )
        self.assertLess(row["odds_decimal_executable"], row["odds_decimal"])
        self.assertLess(row["executable_ev_pct"], row["ev_pct"])
        self.assertEqual(row["execution_levels_used"], 2)
        self.assertGreater(result["diagnostics"]["liquidity_slippage_applied"], 0)


if __name__ == "__main__":
    unittest.main()
