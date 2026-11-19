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
    def test_peak_window_is_five_to_eight_eastern(self):
        self.assertTrue(worker.in_peak_window(utc(2026, 11, 19, 22, 0)))   # 5:00 PM ET
        self.assertTrue(worker.in_peak_window(utc(2026, 11, 20, 0, 59)))   # 7:59 PM ET
        self.assertFalse(worker.in_peak_window(utc(2026, 11, 20, 1, 0)))   # 8:00 PM ET
        self.assertFalse(worker.in_peak_window(utc(2026, 11, 19, 21, 59))) # 4:59 PM ET


class EvSelectionTests(unittest.TestCase):
    def test_no_ev_alerts_outside_peak_window(self):
        self.assertEqual(worker.select_ev_alerts([ev_opp("DraftKings", 6.0)], {}, OFF_PEAK), [])

    def test_one_book_per_bet_keeps_the_best_price(self):
        candidates = [ev_opp("DraftKings", 4.0), ev_opp("FanDuel", 6.5)]
        chosen = worker.select_ev_alerts(candidates, {}, PEAK)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["sportsbook"], "FanDuel")

    def test_daily_cap_of_one_ev_alert(self):
        candidates = [ev_opp("DraftKings", 6.0, event_id="g1"), ev_opp("DraftKings", 5.0, event_id="g2")]
        chosen = worker.select_ev_alerts(candidates, {}, PEAK)
        self.assertEqual([c["event_id"] for c in chosen], ["g1"])

    def test_daily_cap_is_respected_when_already_used(self):
        history = {worker._daily_key("ev", PEAK): {"count": 1, "timestamp": 0}}
        self.assertEqual(worker.select_ev_alerts([ev_opp("DraftKings", 9.0)], history, PEAK), [])


class ArbSelectionTests(unittest.TestCase):
    def test_roi_must_be_between_one_and_five_percent(self):
        candidates = [arb(0.5, 1.0, event_id="low"), arb(6.0, 9.0, event_id="high"), arb(3.0, 4.0, event_id="ok")]
        chosen = worker.select_arb_alerts(candidates, {}, PEAK)
        self.assertEqual([a["event_id"] for a in chosen], ["ok"])

    def test_profit_is_capped_at_twenty_dollars_with_no_minimum(self):
        candidates = [arb(3.0, 25.0, event_id="too_big"), arb(1.2, 0.5, event_id="tiny")]
        chosen = worker.select_arb_alerts(candidates, {}, PEAK)
        self.assertEqual([a["event_id"] for a in chosen], ["tiny"])

    def test_two_arbs_per_day_maximum(self):
        candidates = [arb(r, 5.0, event_id=f"e{i}") for i, r in enumerate([4.0, 3.5, 2.0])]
        chosen = worker.select_arb_alerts(candidates, {}, PEAK)
        self.assertEqual(len(chosen), 2)

    def test_ncaab_spread_or_total_arb_limited_to_one_per_day(self):
        candidates = [
            arb(4.0, 5.0, sport="NCAAB", market="Spread", event_id="n1"),
            arb(3.0, 5.0, sport="NCAAB", market="Total", event_id="n2"),
        ]
        chosen = worker.select_arb_alerts(candidates, {}, PEAK)
        self.assertEqual([a["event_id"] for a in chosen], ["n1"])

    def test_ncaab_limit_already_used_blocks_further_ncaab_spread_total_arbs(self):
        history = {worker._daily_key("ncaab_spread_total_arb", PEAK): {"count": 1, "timestamp": 0}}
        candidates = [arb(4.0, 5.0, sport="NCAAB", market="Spread", event_id="n1")]
        self.assertEqual(worker.select_arb_alerts(candidates, history, PEAK), [])

    def test_ncaab_moneyline_is_not_restricted_by_the_spread_total_limit(self):
        candidates = [arb(4.0, 5.0, sport="NCAAB", market="Moneyline", event_id="m1")]
        chosen = worker.select_arb_alerts(candidates, {}, PEAK)
        self.assertEqual(len(chosen), 1)

    def test_daily_counts_increment_for_arbs_and_ncaab_restricted(self):
        history = {}
        worker.increment_daily_arb_counts(history, arb(4.0, 5.0, sport="NCAAB", market="Spread"), PEAK, 1.0)
        self.assertEqual(worker.daily_count(history, "arb", PEAK), 1)
        self.assertEqual(worker.daily_count(history, "ncaab_spread_total_arb", PEAK), 1)


class CantMissTests(unittest.TestCase):
    def test_cant_miss_markets_for_nfl_and_nba_only(self):
        self.assertTrue(worker.is_cant_miss("Spread", "NBA"))
        self.assertTrue(worker.is_cant_miss("Moneyline", "NFL"))
        self.assertTrue(worker.is_cant_miss("Anytime Touchdown", "NFL"))
        self.assertTrue(worker.is_cant_miss("Total Rushing Yards", "NFL"))
        self.assertFalse(worker.is_cant_miss("Spread", "NCAAB"))
        self.assertFalse(worker.is_cant_miss("Spread", "NHL"))


if __name__ == "__main__":
    unittest.main()
