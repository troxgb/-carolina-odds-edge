import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

import database
import ev_engine


class OpportunityLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "bets.db")
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        database.init_db()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_scan_log_finalizes_clv_from_latest_matching_prestart_pinnacle_quote(self):
        opportunity = {
            "sport": "NBA",
            "event_id": "game-1",
            "matchup": "Home vs Away",
            "kickoff": "2026-01-01T13:00:00Z",
            "market": "Moneyline",
            "market_key": "h2h",
            "period": "",
            "team": "",
            "selection": "Home",
            "sportsbook": "DraftKings",
            "bookmaker_key": "draftkings",
            "line": None,
            "odds_american": 140,
            "odds_decimal": 2.4,
            "fair_probability": 0.5,
            "ev_pct": 20.0,
            "kelly_stake": 10.0,
            "pinnacle_american_this_side": -110,
            "pinnacle_american_other_side": -110,
            "pinnacle_overround": 1.0476,
            "pinnacle_quote_time": "2026-01-01T11:59:30Z",
            "book_quote_time": "2026-01-01T11:59:30Z",
            "flags": "verify",
        }
        snapshot = {
            "sport": "NBA",
            "event_id": "game-1",
            "market_key": "h2h",
            "period": "",
            "team": "",
            "selection": "Home",
            "point": None,
            "kickoff": "2026-01-01T13:00:00Z",
            "fair_probability": 0.55,
            "pinnacle_american": -120,
            "pinnacle_decimal": 1.833333,
            "overround": 1.04,
            "quote_time": "2026-01-01T12:59:30Z",
        }

        database.log_scan(
            "scan-1",
            "2026-01-01T12:59:35Z",
            [opportunity],
            [snapshot],
        )
        finalized = database.finalize_started_opportunities(
            datetime(2026, 1, 1, 13, 0, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(finalized, 1)

        log = database.load_opportunity_log()
        self.assertEqual(len(log), 1)
        self.assertAlmostEqual(log.loc[0, "closing_fair_probability"], 0.55)
        self.assertAlmostEqual(log.loc[0, "clv_pct"], 32.0)
        self.assertEqual(log.loc[0, "closing_quote_time"], "2026-01-01T12:59:30+00:00")

    def _close_fixture(self, snapshot_quote_time):
        opportunity = {
            "sport": "NBA",
            "event_id": "game-lag",
            "matchup": "Home vs Away",
            "kickoff": "2026-01-01T13:00:00Z",
            "market": "Moneyline",
            "market_key": "h2h",
            "period": "",
            "team": "",
            "selection": "Home",
            "sportsbook": "DraftKings",
            "bookmaker_key": "draftkings",
            "line": None,
            "odds_american": 140,
            "odds_decimal": 2.4,
            "fair_probability": 0.5,
            "ev_pct": 20.0,
            "kelly_stake": 10.0,
            "pinnacle_american_this_side": -110,
            "pinnacle_american_other_side": -110,
            "pinnacle_overround": 1.0476,
            "pinnacle_quote_time": "2026-01-01T11:59:30Z",
            "book_quote_time": "2026-01-01T11:59:30Z",
            "flags": "",
        }
        snapshot = {
            "sport": "NBA",
            "event_id": "game-lag",
            "market_key": "h2h",
            "period": "",
            "team": "",
            "selection": "Home",
            "point": None,
            "kickoff": "2026-01-01T13:00:00Z",
            "fair_probability": 0.55,
            "pinnacle_american": -120,
            "pinnacle_decimal": 1.833333,
            "overround": 1.04,
            "quote_time": snapshot_quote_time,
        }
        database.log_scan("scan-lag", "2026-01-01T11:59:35Z", [opportunity], [snapshot])

    def test_close_from_snapshot_twenty_hours_old_is_not_used(self):
        self._close_fixture("2025-12-31T17:00:00Z")
        finalized = database.finalize_started_opportunities(
            datetime(2026, 1, 1, 13, 0, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(finalized, 0)
        log = database.load_opportunity_log()
        self.assertTrue(pd.isna(log.loc[0, "closing_fair_probability"]))
        self.assertTrue(pd.isna(log.loc[0, "clv_pct"]))
        self.assertIsNone(log.loc[0, "finalized_at"])

    def test_close_from_snapshot_five_minutes_old_is_used(self):
        self._close_fixture("2026-01-01T12:55:00Z")
        finalized = database.finalize_started_opportunities(
            datetime(2026, 1, 1, 13, 0, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(finalized, 1)
        log = database.load_opportunity_log()
        self.assertAlmostEqual(log.loc[0, "closing_fair_probability"], 0.55)

    def test_snapshot_after_kickoff_is_not_used_as_close(self):
        self._close_fixture("2026-01-01T13:00:30Z")
        finalized = database.finalize_started_opportunities(
            datetime(2026, 1, 1, 13, 5, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(finalized, 0)

    def test_init_db_configures_wal_and_busy_timeout(self):
        pragmas = []
        original_connect = sqlite3.connect

        class RecordingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                result = super().execute(sql, parameters)
                if sql in ("PRAGMA journal_mode=WAL;", "PRAGMA busy_timeout=5000;"):
                    pragmas.append((sql, result.fetchone()[0]))
                return result

        def recording_connect(*args, **kwargs):
            kwargs["factory"] = RecordingConnection
            return original_connect(*args, **kwargs)

        with patch.object(database.sqlite3, "connect", side_effect=recording_connect):
            database.init_db()

        self.assertEqual(
            pragmas,
            [
                ("PRAGMA journal_mode=WAL;", "wal"),
                ("PRAGMA busy_timeout=5000;", 5000),
            ],
        )

    def test_log_bets_saves_arbitrage_legs_and_grades_without_clv(self):
        database.log_bets(
            [
                {
                    "mode": "Arbitrage",
                    "sport": "NBA",
                    "matchup": "Home vs Away",
                    "market": "Moneyline",
                    "selection": "Home",
                    "sportsbook": "DraftKings",
                    "odds_american": 110,
                    "odds_decimal": 2.1,
                    "fair_prob_at_bet": None,
                    "expected_ev_pct": None,
                    "stake": 52.0,
                },
                {
                    "mode": "Arbitrage",
                    "sport": "NBA",
                    "matchup": "Home vs Away",
                    "market": "Moneyline",
                    "selection": "Away",
                    "sportsbook": "FanDuel",
                    "odds_american": 105,
                    "odds_decimal": 2.05,
                    "fair_prob_at_bet": None,
                    "expected_ev_pct": None,
                    "stake": 48.0,
                },
            ]
        )

        bets = database.load_bets().sort_values("id").reset_index(drop=True)
        self.assertEqual(len(bets), 2)
        self.assertTrue((bets["status"] == "Pending").all())
        self.assertTrue(bets["expected_ev_pct"].isna().all())
        self.assertEqual(bets["selection"].tolist(), ["Home", "Away"])

        database.update_bet_result(int(bets.loc[0, "id"]), "Won", None, 57.2, None)
        graded_bet = database.load_bets().set_index("id").loc[bets.loc[0, "id"]]
        self.assertEqual(graded_bet["status"], "Won")
        self.assertTrue(pd.isna(graded_bet["closing_odds_decimal"]))
        self.assertTrue(pd.isna(graded_bet["clv_pct"]))

    def test_line_adjusted_clv_uses_closing_pinnacle_lines_at_venue_line(self):
        opportunity = {
            "sport": "NBA",
            "event_id": "game-2",
            "matchup": "Home vs Away",
            "kickoff": "2026-01-01T13:00:00Z",
            "market": "Home -3.5",
            "market_key": "spreads",
            "period": "",
            "team": "",
            "selection": "Home",
            "sportsbook": "DraftKings",
            "bookmaker_key": "draftkings",
            "line": -3.5,
            "pinnacle_line": -2.5,
            "pinnacle_lines_used": "-4.5, -2.5",
            "odds_american": 140,
            "odds_decimal": 2.4,
            "fair_probability": 0.5,
            "ev_pct": 20.0,
            "kelly_stake": 10.0,
            "pinnacle_american_this_side": -110,
            "pinnacle_american_other_side": -110,
            "pinnacle_overround": 1.04,
            "pinnacle_quote_time": "2026-01-01T11:59:30Z",
            "book_quote_time": "2026-01-01T11:59:30Z",
            "flags": "line-adjusted",
            "pricing_method": "line-adjusted",
            "line_adjustment_method": "interpolation",
            "win_probability": 0.5,
            "lose_probability": 0.5,
            "push_probability": 0.0,
        }
        snapshots = [
            {
                "sport": "NBA",
                "event_id": "game-2",
                "market_key": "spreads",
                "period": "",
                "team": "",
                "selection": "Home",
                "point": point,
                "kickoff": "2026-01-01T13:00:00Z",
                "fair_probability": probability,
                "pinnacle_american": -110,
                "pinnacle_decimal": 1.9,
                "overround": 1.04,
                "quote_time": "2026-01-01T12:59:30Z",
            }
            for point, probability in ((-2.5, 0.6), (-4.5, 0.4))
        ]

        database.log_scan(
            "scan-adjusted",
            "2026-01-01T12:59:35Z",
            [opportunity],
            snapshots,
        )
        finalized = database.finalize_started_opportunities(
            datetime(2026, 1, 1, 13, 0, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(finalized, 1)

        log = database.load_opportunity_log()
        self.assertEqual(log.loc[0, "pricing_method"], "line-adjusted")
        self.assertEqual(log.loc[0, "line_adjustment_method"], "interpolation")
        self.assertAlmostEqual(log.loc[0, "closing_fair_probability"], 0.5)
        self.assertAlmostEqual(log.loc[0, "clv_pct"], 20.0)


class ClvEdgeSummaryTests(unittest.TestCase):
    def _row(self, observed_at, clv, ev, line=None, selection="Home"):
        return {
            "observed_at": observed_at,
            "sport": "NBA",
            "event_id": "game-1",
            "market": "Spread",
            "period": "",
            "selection": selection,
            "sportsbook": "DraftKings",
            "line": line,
            "ev_pct": ev,
            "clv_pct": clv,
        }

    def test_repeat_scans_of_one_edge_are_counted_once_using_first_observation(self):
        log = pd.DataFrame(
            [
                self._row("2026-01-01T10:00:00+00:00", 5.0, 3.0, line=-3.5),
                self._row("2026-01-01T11:00:00+00:00", 9.0, 8.0, line=-3.5),
                self._row("2026-01-01T10:30:00+00:00", -1.0, 2.0, line=-3.5, selection="Away"),
            ]
        )
        summary = database.summarize_clv_edges(log)
        self.assertEqual(summary["logged"], 2)
        self.assertEqual(summary["closed"], 2)
        self.assertAlmostEqual(summary["mean_clv_pct"], 2.0)
        self.assertAlmostEqual(summary["mean_ev_pct"], 2.5)

    def test_unclosed_edges_are_excluded_from_means_but_still_logged(self):
        log = pd.DataFrame(
            [
                self._row("2026-01-01T10:00:00+00:00", None, 4.0, selection="Home"),
                self._row("2026-01-01T10:00:00+00:00", 6.0, 2.0, selection="Away"),
            ]
        )
        summary = database.summarize_clv_edges(log)
        self.assertEqual(summary["logged"], 2)
        self.assertEqual(summary["closed"], 1)
        self.assertAlmostEqual(summary["mean_clv_pct"], 6.0)
        self.assertAlmostEqual(summary["mean_ev_pct"], 2.0)

    def test_empty_log_and_no_closed_rows_return_none_means(self):
        self.assertEqual(
            database.summarize_clv_edges(pd.DataFrame())["mean_clv_pct"], None
        )
        log = pd.DataFrame([self._row("2026-01-01T10:00:00+00:00", None, 4.0)])
        summary = database.summarize_clv_edges(log)
        self.assertEqual(summary["closed"], 0)
        self.assertIsNone(summary["mean_clv_pct"])
        self.assertIsNone(summary["mean_ev_pct"])

    def test_load_opportunity_log_exposes_event_id_and_commence_time(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        with patch.object(database, "DB_PATH", os.path.join(temp_dir.name, "b.db")):
            database.init_db()
            log = database.load_opportunity_log()
        self.assertIn("event_id", log.columns)
        self.assertIn("commence_time", log.columns)


class BetClvBasisTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "bets.db")
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        database.init_db()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def _bet(self):
        return {
            "mode": "Positive EV",
            "sport": "NBA",
            "matchup": "Home vs Away",
            "market": "Moneyline",
            "selection": "Home",
            "sportsbook": "DraftKings",
            "odds_american": 110,
            "odds_decimal": 2.1,
            "fair_prob_at_bet": 0.5,
            "expected_ev_pct": 5.0,
            "stake": 10.0,
        }

    def test_new_bets_are_marked_novig_and_graded_bets_stay_novig(self):
        database.log_bet(self._bet())
        self.assertEqual(database.load_bets().loc[0, "clv_basis"], "novig")
        database.update_bet_result(1, "Won", 1.95, 11.0, 2.0)
        self.assertEqual(database.load_bets().loc[0, "clv_basis"], "novig")

    def test_legacy_bets_without_column_are_migrated_as_vigged_price(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("ALTER TABLE bets DROP COLUMN clv_basis")
            conn.execute(
                """
                INSERT INTO bets (timestamp, mode, sport, odds_decimal, stake,
                                  status, profit_loss, clv_pct)
                VALUES ('2025-12-01 10:00:00', 'Positive EV', 'NBA', 2.1, 10.0,
                        'Won', 11.0, 3.0)
                """
            )
        database.init_db()
        bets = database.load_bets()
        self.assertEqual(bets.loc[0, "clv_basis"], "vigged_price")
        self.assertAlmostEqual(bets.loc[0, "clv_pct"], 3.0)


class BetClvFormulaTests(unittest.TestCase):
    def test_no_vig_clv_uses_devigged_closing_probability(self):
        # Symmetric -110 closes: fair probability 0.5, bet price 2.1.
        clv = ev_engine.bet_clv_pct(2.1, 1.9090909091, 1.9090909091, "power")
        self.assertAlmostEqual(clv, 5.0, places=4)

    def test_no_vig_clv_differs_from_vigged_shortcut(self):
        clv = ev_engine.bet_clv_pct(2.1, 1.8, 2.1, "multiplicative")
        vigged = (2.1 * (1 / 1.8) - 1) * 100
        self.assertNotAlmostEqual(clv, vigged, places=2)

    def test_closing_odds_for_both_sides_are_required(self):
        with self.assertRaises(ValueError):
            ev_engine.bet_clv_pct(2.1, 1.9, None)
        with self.assertRaises(ValueError):
            ev_engine.bet_clv_pct(2.1, None, 1.9)


class BetTimestampAndStoredStakeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "bets.db")
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        database.init_db()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_logged_bets_use_utc_iso_timestamps(self):
        database.log_bet(
            {"mode": "Positive EV", "sport": "NBA", "odds_decimal": 2.0, "stake": 5.0}
        )
        timestamp = database.load_bets().loc[0, "timestamp"]
        parsed = datetime.fromisoformat(timestamp)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)
        self.assertEqual(timestamp, parsed.isoformat())

    def test_logged_kelly_stake_is_the_post_cap_value_including_verify_rules(self):
        from ev_engine import calculate_capped_kelly_stakes

        row = {
            "sport": "NBA",
            "event_id": "game-s",
            "matchup": "Home vs Away",
            "kickoff": "2026-01-01T13:00:00Z",
            "market": "Moneyline",
            "market_key": "h2h",
            "period": "",
            "team": "",
            "selection": "Home",
            "sportsbook": "DraftKings",
            "bookmaker_key": "draftkings",
            "line": None,
            "odds_american": 150,
            "odds_decimal": 2.5,
            "fair_probability": 0.6,
            "ev_pct": 50.0,
            "pinnacle_american_this_side": -110,
            "pinnacle_american_other_side": -110,
            "pinnacle_overround": 1.04,
            "pinnacle_quote_time": "2026-01-01T11:59:30Z",
            "book_quote_time": "2026-01-01T11:59:30Z",
            "flags": "verify",
        }
        stake = calculate_capped_kelly_stakes([row], bankroll=1000, fraction=0.25)[0]
        row["kelly_stake"] = stake
        database.log_scan("scan-stake", "2026-01-01T11:59:35Z", [row], [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            stored = conn.execute(
                "SELECT kelly_stake FROM ev_opportunity_log WHERE scan_id = 'scan-stake'"
            ).fetchone()[0]
        self.assertEqual(stored, 0.0)


class AlertLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "bets.db")
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        database.init_db()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_log_alerts_persists_dispatched_rows(self):
        database.log_alerts(
            [
                {
                    "ts_utc": "2026-11-24T20:00:00+00:00",
                    "et_band": "peak",
                    "kind": "ev",
                    "alert_key": "ev-key",
                    "sport": "NBA",
                    "event_id": "game-1",
                    "market": "Moneyline",
                    "market_key": "h2h",
                    "selection": "Home",
                    "odds_american": 140,
                    "ev_pct": 5.2,
                    "executable_ev_pct": 4.8,
                    "minutes_to_kickoff": 117.5,
                },
                {
                    "ts_utc": "2026-11-25T03:00:00+00:00",
                    "et_band": "off",
                    "kind": "arb",
                    "alert_key": "arb-key",
                    "sport": "NCAAB",
                    "event_id": "game-2",
                    "roi_pct": 2.1,
                },
            ]
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("SELECT * FROM alert_ledger ORDER BY id")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["et_band"], "peak")
        self.assertEqual(rows[0]["kind"], "ev")
        self.assertAlmostEqual(rows[0]["ev_pct"], 5.2)
        self.assertAlmostEqual(rows[0]["executable_ev_pct"], 4.8)
        self.assertAlmostEqual(rows[0]["minutes_to_kickoff"], 117.5)
        self.assertEqual(rows[1]["et_band"], "off")
        self.assertEqual(rows[1]["kind"], "arb")
        self.assertAlmostEqual(rows[1]["roi_pct"], 2.1)
        self.assertIsNone(rows[1]["minutes_to_kickoff"])

    def test_log_alerts_empty_is_noop(self):
        database.log_alerts([])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM alert_ledger").fetchone()[0], 0
            )

    def _closing_fixture(
        self,
        event_id,
        scan_id,
        kickoff="2026-01-01T13:00:00Z",
        quote_time="2026-01-01T12:59:30Z",
    ):
        opportunity = {
            "sport": "NBA",
            "event_id": event_id,
            "matchup": "Home vs Away",
            "kickoff": kickoff,
            "market": "Moneyline",
            "market_key": "h2h",
            "period": "",
            "team": "",
            "selection": "Home",
            "sportsbook": "DraftKings",
            "bookmaker_key": "draftkings",
            "line": None,
            "odds_american": 140,
            "odds_decimal": 2.4,
            "fair_probability": 0.5,
            "ev_pct": 20.0,
            "kelly_stake": 10.0,
            "pinnacle_american_this_side": -110,
            "pinnacle_american_other_side": -110,
            "pinnacle_overround": 1.0476,
            "pinnacle_quote_time": quote_time,
            "book_quote_time": quote_time,
            "flags": "",
        }
        snapshot = {
            "sport": "NBA",
            "event_id": event_id,
            "market_key": "h2h",
            "period": "",
            "team": "",
            "selection": "Home",
            "point": None,
            "kickoff": kickoff,
            "fair_probability": 0.55,
            "pinnacle_american": -120,
            "pinnacle_decimal": 1.833333,
            "overround": 1.04,
            "quote_time": quote_time,
        }
        database.log_scan(scan_id, "2026-01-01T12:59:35Z", [opportunity], [snapshot])

    def test_finalize_processes_newest_pending_rows_first(self):
        self._closing_fixture("game-old", "scan-old")
        self._closing_fixture(
            "game-new",
            "scan-new",
            kickoff="2026-01-02T13:00:00Z",
            quote_time="2026-01-02T12:59:30Z",
        )
        now = datetime(2026, 1, 3, 13, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(database.finalize_started_opportunities(now, limit=1), 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            done = [
                r[0]
                for r in conn.execute(
                    "SELECT event_id FROM ev_opportunity_log "
                    "WHERE finalized_at IS NOT NULL"
                )
            ]
        self.assertEqual(done, ["game-new"])

    def test_finalize_limit_bounds_rows_per_call(self):
        self._closing_fixture("game-a", "scan-a")
        self._closing_fixture("game-b", "scan-b")
        now = datetime(2026, 1, 1, 13, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(database.finalize_started_opportunities(now, limit=1), 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            done = conn.execute(
                "SELECT COUNT(*) FROM ev_opportunity_log WHERE finalized_at IS NOT NULL"
            ).fetchone()[0]
        self.assertEqual(done, 1)
        self.assertEqual(database.finalize_started_opportunities(now), 1)


if __name__ == "__main__":
    unittest.main()
