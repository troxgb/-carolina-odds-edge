import unittest
from datetime import datetime, timezone

import worker


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def ev_opp(book, ev, market="Spread", selection="Home", line=-3.5, event_id="g1", odds=150):
    return {
        "event_id": event_id,
        "market": market,
        "selection": selection,
        "line": line,
        "sportsbook": book,
        "ev_pct": ev,
        "odds_american": odds,
        "sport": "NBA",
    }


def arb(roi, profit, sport="NBA", market="Spread", event_id="a1"):
    return {
        "event_id": event_id,
        "market": market,
        "sport": sport,
        "roi_pct": roi,
        "profit": profit,
        "legs": [],
    }


PEAK = utc(2026, 11, 19, 22, 30)  # 5:30 PM Eastern
OFF_PEAK = utc(2026, 11, 19, 19, 30)  # 2:30 PM Eastern


class OddsRangeTests(unittest.TestCase):
    def test_range_is_inclusive_from_minus_150_to_plus_200(self):
        self.assertTrue(worker.in_ev_odds_range({"odds_american": -150}))
        self.assertTrue(worker.in_ev_odds_range({"odds_american": -110}))
        self.assertTrue(worker.in_ev_odds_range({"odds_american": 200}))
        self.assertFalse(worker.in_ev_odds_range({"odds_american": -151}))
        self.assertFalse(worker.in_ev_odds_range({"odds_american": -200}))
        self.assertFalse(worker.in_ev_odds_range({"odds_american": 201}))

    def test_missing_or_bad_odds_are_rejected(self):
        self.assertFalse(worker.in_ev_odds_range({}))
        self.assertFalse(worker.in_ev_odds_range({"odds_american": "n/a"}))


class PeakWindowTests(unittest.TestCase):
    def test_peak_window_is_three_to_eight_eastern_on_weekdays(self):
        self.assertTrue(worker.in_peak_window(utc(2026, 11, 19, 20, 0)))   # 3:00 PM ET
        self.assertTrue(worker.in_peak_window(utc(2026, 11, 19, 22, 0)))   # 5:00 PM ET
        self.assertTrue(worker.in_peak_window(utc(2026, 11, 20, 0, 59)))   # 7:59 PM ET
        self.assertFalse(worker.in_peak_window(utc(2026, 11, 20, 1, 0)))   # 8:00 PM ET
        self.assertFalse(worker.in_peak_window(utc(2026, 11, 19, 19, 59))) # 2:59 PM ET

    def test_peak_window_is_all_day_on_weekends(self):
        # Saturday Nov 21, 2026
        self.assertTrue(worker.in_peak_window(utc(2026, 11, 21, 14, 0)))   # 9:00 AM ET
        self.assertTrue(worker.in_peak_window(utc(2026, 11, 21, 22, 0)))   # 5:00 PM ET
        self.assertFalse(worker.in_peak_window(utc(2026, 11, 21, 13, 0)))  # 8:00 AM ET (before 9)


class EvSelectionTests(unittest.TestCase):
    def test_no_ev_alerts_outside_peak_window(self):
        self.assertEqual(worker.select_ev_alerts([ev_opp("DraftKings", 6.0)], {}, OFF_PEAK), [])

    def test_one_book_per_bet_keeps_the_best_price(self):
        candidates = [ev_opp("DraftKings", 4.0), ev_opp("FanDuel", 6.5)]
        chosen = worker.select_ev_alerts(candidates, {}, PEAK)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["sportsbook"], "FanDuel")

    def test_multiple_ev_alerts_allowed_per_day(self):
        candidates = [ev_opp("DraftKings", 6.0, event_id="g1"), ev_opp("DraftKings", 5.0, event_id="g2")]
        chosen = worker.select_ev_alerts(candidates, {}, PEAK)
        self.assertEqual([c["event_id"] for c in chosen], ["g1", "g2"])

    def test_daily_cap_removed_allows_ev_alerts(self):
        history = {worker._daily_key("ev", PEAK): {"count": 1, "timestamp": 0}}
        self.assertEqual(len(worker.select_ev_alerts([ev_opp("DraftKings", 9.0)], history, PEAK)), 1)


class ArbSelectionTests(unittest.TestCase):
    def test_roi_must_be_at_least_one_percent(self):
        candidates = [arb(0.5, 25.0, event_id="low"), arb(6.0, 25.0, event_id="high"), arb(3.0, 25.0, event_id="ok")]
        chosen = worker.select_arb_alerts(candidates, {}, PEAK)
        self.assertEqual([a["event_id"] for a in chosen], ["high", "ok"])

    def test_profit_must_be_at_least_twenty_dollars(self):
        candidates = [arb(3.0, 25.0, event_id="big_enough"), arb(1.2, 0.5, event_id="too_small")]
        chosen = worker.select_arb_alerts(candidates, {}, PEAK)
        self.assertEqual([a["event_id"] for a in chosen], ["big_enough"])

    def test_all_eligible_arbs_are_returned(self):
        candidates = [arb(r, 25.0, event_id=f"e{i}") for i, r in enumerate([4.0, 3.5, 2.0])]
        chosen = worker.select_arb_alerts(candidates, {}, PEAK)
        self.assertEqual(len(chosen), 3)

    def test_ncaab_spread_and_total_arbs_are_not_capped(self):
        candidates = [
            arb(4.0, 25.0, sport="NCAAB", market="Spread", event_id="n1"),
            arb(3.0, 25.0, sport="NCAAB", market="Total", event_id="n2"),
        ]
        chosen = worker.select_arb_alerts(candidates, {}, PEAK)
        self.assertEqual([a["event_id"] for a in chosen], ["n1", "n2"])

    def test_ncaab_moneyline_is_not_restricted(self):
        candidates = [arb(4.0, 25.0, sport="NCAAB", market="Moneyline", event_id="m1")]
        chosen = worker.select_arb_alerts(candidates, {}, PEAK)
        self.assertEqual(len(chosen), 1)

    def test_daily_counts_increment_for_arbs(self):
        history = {}
        worker.increment_daily_arb_counts(history, arb(4.0, 25.0, sport="NCAAB", market="Spread"), PEAK, 1.0)
        self.assertEqual(worker.daily_count(history, "arb", PEAK), 1)


class CantMissTests(unittest.TestCase):
    def test_cant_miss_markets_for_nfl_and_nba_only(self):
        self.assertTrue(worker.is_cant_miss("Spread", "NBA"))
        self.assertTrue(worker.is_cant_miss("Moneyline", "NFL"))
        self.assertTrue(worker.is_cant_miss("Anytime Touchdown", "NFL"))
        self.assertTrue(worker.is_cant_miss("Total Rushing Yards", "NFL"))
        self.assertFalse(worker.is_cant_miss("Spread", "NCAAB"))
        self.assertFalse(worker.is_cant_miss("Spread", "NHL"))


class SpreadFloorTests(unittest.TestCase):
    def test_spreads_need_the_higher_floor(self):
        spread = {"market_key": "spreads", "market": "Spread -2.5"}
        self.assertEqual(worker.ev_floor_for(spread, 3.5), 4.5)
        # A caller-supplied floor above the spread minimum still wins.
        self.assertEqual(worker.ev_floor_for(spread, 6.0), 6.0)

    def test_other_markets_keep_the_configured_floor(self):
        moneyline = {"market_key": "h2h", "market": "Moneyline"}
        total = {"market_key": "totals", "market": "Total 210.5"}
        self.assertEqual(worker.ev_floor_for(moneyline, 3.5), 3.5)
        self.assertEqual(worker.ev_floor_for(total, 3.5), 3.5)

    def test_market_text_only_spread_is_recognized(self):
        self.assertTrue(worker._is_spread_quote({"market": "Spread -1.5"}))
        self.assertFalse(worker._is_spread_quote({"market": "Total 210.5"}))


class AlertBandTests(unittest.TestCase):
    def test_bands_follow_the_alert_window(self):
        # Thursday Nov 19, 2026 is an ET weekday (EST = UTC-5).
        self.assertEqual(worker.alert_band(utc(2026, 11, 19, 20, 0)), "peak")  # 3 PM ET
        self.assertEqual(worker.alert_band(utc(2026, 11, 19, 17, 30)), "shoulder")  # 12:30 PM ET
        self.assertEqual(worker.alert_band(utc(2026, 11, 20, 2, 0)), "shoulder")  # 9 PM ET
        self.assertEqual(worker.alert_band(utc(2026, 11, 20, 7, 0)), "off")  # 2 AM ET
        # Saturday Nov 21, 2026 is a weekend day.
        self.assertEqual(worker.alert_band(utc(2026, 11, 21, 14, 0)), "peak")  # 9 AM ET
        self.assertEqual(worker.alert_band(utc(2026, 11, 21, 12, 0)), "shoulder")  # 7 AM ET
        self.assertEqual(worker.alert_band(utc(2026, 11, 22, 4, 0)), "shoulder")  # 11 PM ET


if __name__ == "__main__":
    unittest.main()
