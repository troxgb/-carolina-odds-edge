import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import worker


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


NOW = utc(2026, 11, 19, 22, 0)  # Thursday 5:00 PM ET


def kickoff_in(**delta):
    return (NOW + timedelta(**delta)).isoformat()


def ev_opp(
    book,
    ev,
    market="Spread",
    selection="Home",
    line=-3.5,
    event_id="g1",
    odds=150,
    kickoff=None,
):
    return {
        "event_id": event_id,
        "market": market,
        "selection": selection,
        "line": line,
        "sportsbook": book,
        "ev_pct": ev,
        "odds_american": odds,
        "sport": "NBA",
        "kickoff": kickoff if kickoff is not None else kickoff_in(hours=2),
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


class EvAlertWindowTests(unittest.TestCase):
    def test_per_bet_lead_and_guard_boundaries(self):
        def opp(minutes):
            return ev_opp(
                "DraftKings",
                5.0,
                kickoff=(NOW + timedelta(minutes=minutes)).isoformat(),
            )

        self.assertTrue(worker.in_ev_alert_window(opp(180), NOW))
        self.assertFalse(worker.in_ev_alert_window(opp(181), NOW))
        self.assertTrue(worker.in_ev_alert_window(opp(30), NOW))
        self.assertFalse(worker.in_ev_alert_window(opp(29), NOW))
        self.assertFalse(worker.in_ev_alert_window(opp(-1), NOW))

    def test_missing_or_bad_kickoff_is_rejected(self):
        missing = ev_opp("DraftKings", 5.0)
        missing.pop("kickoff")
        bad = ev_opp("DraftKings", 5.0, kickoff="not-a-date")
        self.assertFalse(worker.in_ev_alert_window(missing, NOW))
        self.assertFalse(worker.in_ev_alert_window(bad, NOW))


class EvSlateTests(unittest.TestCase):
    def test_slate_uses_first_and_last_game_of_the_day(self):
        sources = [
            {"event_id": "early", "kickoff": utc(2026, 11, 19, 18, 0).isoformat()},
            {"event_id": "mid", "kickoff": utc(2026, 11, 20, 0, 0).isoformat()},
            {"event_id": "late", "kickoff": utc(2026, 11, 20, 3, 0).isoformat()},
        ]
        slates = worker.build_ev_alert_slates(sources)
        self.assertEqual(len(slates), 1)
        slate = next(iter(slates.values()))
        self.assertEqual(slate.day, "2026-11-19")
        self.assertEqual(slate.first_kickoff, utc(2026, 11, 19, 18, 0))
        self.assertEqual(slate.last_kickoff, utc(2026, 11, 20, 3, 0))
        self.assertEqual(slate.open_at, utc(2026, 11, 19, 15, 0))
        self.assertEqual(slate.close_at, utc(2026, 11, 20, 2, 30))

    def test_one_am_game_stays_with_the_prior_evening(self):
        one_am = utc(2026, 11, 20, 6, 0)  # 1:00 AM ET
        sources = [
            {"event_id": "early", "kickoff": utc(2026, 11, 19, 18, 0).isoformat()},
            {"event_id": "late", "kickoff": one_am.isoformat()},
        ]
        slates = worker.build_ev_alert_slates(sources)
        slate = next(iter(slates.values()))
        self.assertEqual(slate.day, "2026-11-19")
        self.assertEqual(slate.last_kickoff, one_am)
        self.assertEqual(slate.close_at, utc(2026, 11, 20, 5, 30))

        opp = ev_opp("DraftKings", 5.0, kickoff=one_am.isoformat())
        # 10:00 PM ET: exactly 3h before kickoff, both windows open.
        self.assertTrue(
            worker.ev_alert_eligible(opp, slates, utc(2026, 11, 20, 3, 0))
        )
        # 11:00 PM ET: still inside the lead and the slate close.
        self.assertTrue(
            worker.ev_alert_eligible(opp, slates, utc(2026, 11, 20, 4, 0))
        )
        # 9:30 PM ET: 3.5h before kickoff, outside the per-bet lead.
        self.assertFalse(
            worker.ev_alert_eligible(opp, slates, utc(2026, 11, 20, 2, 30))
        )

    def test_next_morning_game_is_not_alerted_tonight(self):
        tomorrow_7am = utc(2026, 11, 20, 12, 0)  # 7:00 AM ET Nov 20
        slates = worker.build_ev_alert_slates(
            [{"event_id": "morning", "kickoff": tomorrow_7am.isoformat()}]
        )
        opp = ev_opp("DraftKings", 5.0, kickoff=tomorrow_7am.isoformat())
        self.assertFalse(worker.ev_alert_eligible(opp, slates, NOW))
        self.assertTrue(
            worker.ev_alert_eligible(opp, slates, utc(2026, 11, 20, 9, 30))
        )

    def test_six_am_boundary_splits_betting_days(self):
        five_am = utc(2026, 11, 20, 10, 0)  # 5:00 AM ET, prior betting day
        seven_am = utc(2026, 11, 20, 12, 0)  # 7:00 AM ET, new betting day
        slates = worker.build_ev_alert_slates(
            [
                {"event_id": "five", "kickoff": five_am.isoformat()},
                {"event_id": "seven", "kickoff": seven_am.isoformat()},
            ]
        )
        self.assertEqual(
            sorted(slate.day for slate in slates.values()),
            ["2026-11-19", "2026-11-20"],
        )

    def test_shorter_slate_lead_delays_the_first_game(self):
        early = utc(2026, 11, 19, 18, 0)  # 1:00 PM ET
        late = utc(2026, 11, 20, 0, 0)  # 7:00 PM ET
        sources = [
            {"event_id": "early", "kickoff": early.isoformat()},
            {"event_id": "late", "kickoff": late.isoformat()},
        ]
        opp = ev_opp("DraftKings", 5.0, kickoff=early.isoformat())
        eleven_am = utc(2026, 11, 19, 16, 0)

        self.assertTrue(
            worker.ev_alert_eligible(
                opp, worker.build_ev_alert_slates(sources), eleven_am
            )
        )
        with patch.object(worker, "SLATE_LEAD_HOURS", 1.0):
            short_slates = worker.build_ev_alert_slates(sources)
            self.assertFalse(
                worker.ev_alert_eligible(opp, short_slates, eleven_am)
            )
            self.assertTrue(
                worker.ev_alert_eligible(
                    opp, short_slates, utc(2026, 11, 19, 17, 15)
                )
            )

    def test_no_games_means_no_slate_and_no_alerts(self):
        self.assertEqual(worker.build_ev_alert_slates([]), {})
        self.assertEqual(worker.select_ev_alerts([], {}, NOW), [])


class EvSelectionTests(unittest.TestCase):
    def test_no_ev_alerts_outside_the_per_bet_window(self):
        candidates = [
            ev_opp("DraftKings", 6.0, kickoff=kickoff_in(hours=5))
        ]
        self.assertEqual(worker.select_ev_alerts(candidates, {}, NOW), [])

    def test_one_book_per_bet_keeps_the_best_price(self):
        candidates = [ev_opp("DraftKings", 4.0), ev_opp("FanDuel", 6.5)]
        chosen = worker.select_ev_alerts(candidates, {}, NOW)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["sportsbook"], "FanDuel")

    def test_multiple_ev_alerts_allowed_per_day(self):
        candidates = [
            ev_opp("DraftKings", 6.0, event_id="g1"),
            ev_opp("DraftKings", 5.0, event_id="g2"),
        ]
        chosen = worker.select_ev_alerts(candidates, {}, NOW)
        self.assertEqual([c["event_id"] for c in chosen], ["g1", "g2"])

    def test_daily_cap_removed_allows_ev_alerts(self):
        history = {worker._daily_key("ev", NOW): {"count": 1, "timestamp": 0}}
        self.assertEqual(
            len(worker.select_ev_alerts([ev_opp("DraftKings", 9.0)], history, NOW)),
            1,
        )

    def test_missing_kickoff_is_never_selected(self):
        candidate = ev_opp("DraftKings", 9.0)
        candidate.pop("kickoff")
        self.assertEqual(worker.select_ev_alerts([candidate], {}, NOW), [])

    def test_a_late_game_is_blocked_while_a_near_game_is_selected(self):
        candidates = [
            ev_opp("DraftKings", 6.0, event_id="near", kickoff=kickoff_in(hours=1)),
            ev_opp("FanDuel", 5.0, event_id="far", kickoff=kickoff_in(hours=5)),
        ]
        chosen = worker.select_ev_alerts(candidates, {}, NOW)
        self.assertEqual([c["event_id"] for c in chosen], ["near"])


class ArbSelectionTests(unittest.TestCase):
    def test_roi_must_be_at_least_one_percent(self):
        candidates = [
            arb(0.5, 25.0, event_id="low"),
            arb(6.0, 25.0, event_id="high"),
            arb(3.0, 25.0, event_id="ok"),
        ]
        chosen = worker.select_arb_alerts(candidates, {}, NOW)
        self.assertEqual([a["event_id"] for a in chosen], ["high", "ok"])

    def test_profit_must_be_at_least_ten_dollars(self):
        candidates = [
            arb(3.0, 25.0, event_id="big_enough"),
            arb(1.2, 0.5, event_id="too_small"),
        ]
        chosen = worker.select_arb_alerts(candidates, {}, NOW)
        self.assertEqual([a["event_id"] for a in chosen], ["big_enough"])

    def test_all_eligible_arbs_are_returned(self):
        candidates = [
            arb(r, 25.0, event_id=f"e{i}")
            for i, r in enumerate([4.0, 3.5, 2.0])
        ]
        chosen = worker.select_arb_alerts(candidates, {}, NOW)
        self.assertEqual(len(chosen), 3)

    def test_ncaab_spread_and_total_arbs_are_not_capped(self):
        candidates = [
            arb(4.0, 25.0, sport="NCAAB", market="Spread", event_id="n1"),
            arb(3.0, 25.0, sport="NCAAB", market="Total", event_id="n2"),
        ]
        chosen = worker.select_arb_alerts(candidates, {}, NOW)
        self.assertEqual([a["event_id"] for a in chosen], ["n1", "n2"])

    def test_ncaab_moneyline_is_not_restricted(self):
        candidates = [
            arb(4.0, 25.0, sport="NCAAB", market="Moneyline", event_id="m1")
        ]
        chosen = worker.select_arb_alerts(candidates, {}, NOW)
        self.assertEqual(len(chosen), 1)

    def test_daily_counts_increment_for_arbs(self):
        history = {}
        worker.increment_daily_arb_counts(
            history, arb(4.0, 25.0, sport="NCAAB", market="Spread"), NOW, 1.0
        )
        self.assertEqual(worker.daily_count(history, "arb", NOW), 1)


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
    def test_et_band_is_ledger_metadata_only(self):
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
