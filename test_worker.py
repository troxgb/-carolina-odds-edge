import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from worker import (
    ACTIVE_SPORTS,
    DEFAULT_MAX_EV_THRESHOLD,
    DEFAULT_MIN_ARB_ROI,
    DEFAULT_MIN_EV_THRESHOLD,
    MAX_HISTORY_AGE_SECONDS,
    MAX_SINGLE_BET,
    _atomic_write_json,
    apply_arbitrage_size_limits,
    filter_eligible_arbs,
    build_alert_key,
    build_arbitrage_alert_key,
    check_and_welcome_new_members,
    clean_expired_history,
    fetch_odds_with_fallback,
    format_kelly_units,
    format_telegram_alert,
    format_telegram_arbitrage_alert,
    format_welcome_guide,
    get_api_keys,
    is_game_started,
    load_alert_history,
    load_cached_odds,
    run_worker,
    save_alert_history,
    save_cached_odds,
    send_telegram_message,
    should_alert,
    should_alert_arbitrage,
    worker_ev_settings,
)
from scanner_config import CONFIG


class WorkerTests(unittest.TestCase):
    def test_active_sports_configuration(self):
        self.assertEqual(
            ACTIVE_SPORTS,
            ["NCAAB", "NBA", "WNBA", "NHL", "NFL", "NCAAF", "UFC"],
        )
        self.assertNotIn("MLB", ACTIVE_SPORTS)

    def test_max_single_bet_cap_constant(self):
        self.assertEqual(MAX_SINGLE_BET, 300.0)
        self.assertEqual(DEFAULT_MIN_ARB_ROI, 1.0)

    def test_freshness_windows_are_split(self):
        # API payload stays small; local scan tolerates PropLine's typical
        # 2-5 minute last_update lag.
        self.assertEqual(CONFIG.api_max_age_seconds, 60)
        self.assertEqual(CONFIG.max_quote_age_seconds, 300)

    def test_filter_eligible_arbs_drops_profit_below_floor_after_cap(self):
        now = datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)
        kickoff = (now + timedelta(hours=3)).isoformat()

        def arb(odds):
            return {
                "kickoff": kickoff,
                "total_stake": 600,
                "roi_pct": 5.0,
                "legs": [
                    {"odds_decimal": odds, "stake": 300},
                    {"odds_decimal": odds, "stake": 300},
                ],
            }

        # 2.05/2.05 is a real 2.4% arb, but $300 total only guarantees ~$7.32,
        # below the $10 floor. 2.40/2.40 clears it comfortably.
        thin = arb(2.05)
        fat = arb(2.40)

        kept = filter_eligible_arbs(
            [thin, fat], now, min_arb_roi=1.0, max_bet_cap=300.0
        )

        self.assertEqual(kept, [fat])
        self.assertLess(thin["profit"], 10.0)
        self.assertGreaterEqual(fat["profit"], 10.0)
        self.assertLessEqual(fat["total_stake"], 300.0)

    def test_is_game_started(self):
        now = datetime(2026, 11, 17, 20, 0, 0, tzinfo=timezone.utc)
        past = (now - timedelta(minutes=5)).isoformat()
        future = (now + timedelta(hours=2)).isoformat()

        self.assertTrue(is_game_started(past, now))
        self.assertTrue(is_game_started(now.isoformat(), now))
        self.assertFalse(is_game_started(future, now))
        self.assertFalse(is_game_started(None, now))
        self.assertFalse(is_game_started("invalid-time", now))

    def test_prestart_guard_blocks_imminent_kickoffs(self):
        # Provider clocks can run ~30 minutes late, so events inside the guard
        # window must be treated as unbetable rather than as upcoming.
        now = datetime(2026, 11, 21, 19, 56, 0, tzinfo=timezone.utc)
        soon = (now + timedelta(minutes=4)).isoformat()
        later = (now + timedelta(minutes=45)).isoformat()

        self.assertFalse(is_game_started(soon, now))
        self.assertTrue(is_game_started(soon, now, guard_minutes=30))
        self.assertFalse(is_game_started(later, now, guard_minutes=30))
        self.assertEqual(CONFIG.prestart_guard_minutes, 30)

    def test_build_alert_key_with_point_and_line(self):
        opp1 = {
            "event_id": "game-101",
            "market": "Spread -3.5",
            "selection": "North Carolina",
            "line": -3.5,
            "sportsbook": "DraftKings",
        }
        self.assertEqual(
            build_alert_key(opp1),
            "game-101_Spread -3.5_North Carolina_-3.5_DraftKings",
        )

        opp2 = {
            "game_id": "game-202",
            "market_key": "h2h",
            "selection": "Duke",
            "sportsbook": "FanDuel",
        }
        self.assertEqual(
            build_alert_key(opp2),
            "game-202_h2h_Duke_None_FanDuel",
        )

    def test_build_arbitrage_alert_key(self):
        arb = {
            "event_id": "game-999",
            "market": "Moneyline",
            "legs": [
                {"selection": "Home", "sportsbook": "FanDuel"},
                {"selection": "Away", "sportsbook": "DraftKings"},
            ],
        }
        key = build_arbitrage_alert_key(arb)
        self.assertEqual(key, "arb_game-999_Moneyline_Away_DraftKings-Home_FanDuel")

    def test_should_alert_new_key(self):
        history = {}
        self.assertTrue(should_alert("key1", 3.5, history))

    def test_should_alert_unchanged_or_lower_ev(self):
        history = {"key1": {"ev": 4.0, "timestamp": time.time()}}
        self.assertFalse(should_alert("key1", 4.0, history))
        self.assertFalse(should_alert("key1", 3.5, history))
        self.assertFalse(should_alert("key1", 5.4, history))

    def test_should_alert_significant_ev_increase(self):
        history = {"key1": {"ev": 4.0, "timestamp": time.time()}}
        self.assertTrue(should_alert("key1", 5.5, history))
        self.assertTrue(should_alert("key1", 6.0, history))

    def test_should_alert_arbitrage(self):
        history = {}
        self.assertTrue(should_alert_arbitrage("arb1", 2.0, history))

        history["arb1"] = {"roi": 2.0, "timestamp": time.time()}
        self.assertFalse(should_alert_arbitrage("arb1", 2.0, history))
        self.assertFalse(should_alert_arbitrage("arb1", 2.3, history))
        self.assertTrue(should_alert_arbitrage("arb1", 2.6, history))

    def test_format_kelly_units_and_cap_at_300(self):
        # Default bankroll is 1000, 1 unit = $10
        self.assertEqual(format_kelly_units(0.0), "0u")
        # Sub-unit positive stake floors to 0.25u minimum
        self.assertEqual(format_kelly_units(1.0), "0.25u")
        # 0.25u increments
        self.assertEqual(format_kelly_units(2.5), "0.25u")
        self.assertEqual(format_kelly_units(5.0), "0.5u")
        self.assertEqual(format_kelly_units(7.5), "0.75u")
        self.assertEqual(format_kelly_units(10.0), "1u")
        self.assertEqual(format_kelly_units(12.5), "1.25u")
        self.assertEqual(format_kelly_units(15.0), "1.5u")
        self.assertEqual(format_kelly_units(17.5), "1.75u")
        self.assertEqual(format_kelly_units(20.0), "2u")
        self.assertEqual(format_kelly_units(300.0), "30u")
        # Over $300 should be capped at 30u ($300)
        self.assertEqual(format_kelly_units(500.0), "30u")

    def test_clean_expired_history(self):
        now = 1000000.0
        history = {
            "fresh": {"ev": 3.5, "timestamp": now - 3600},
            "borderline": {"ev": 4.0, "timestamp": now - MAX_HISTORY_AGE_SECONDS},
            "expired": {"ev": 5.0, "timestamp": now - (MAX_HISTORY_AGE_SECONDS + 10)},
            "malformed": {"ev": 3.0},
        }
        cleaned = clean_expired_history(history, now)
        self.assertIn("fresh", cleaned)
        self.assertIn("borderline", cleaned)
        self.assertNotIn("expired", cleaned)
        self.assertNotIn("malformed", cleaned)

    def test_format_telegram_alert_contains_required_fields_and_caps_at_300(self):
        opp = {
            "sport": "NBA",
            "matchup": "Boston Celtics vs Dallas Mavericks",
            "market": "Spread -4.5",
            "selection": "Boston Celtics",
            "line": -4.5,
            "odds_american": -110,
            "odds_decimal": 1.91,
            "sportsbook": "DraftKings",
            "fair_probability": 0.55,
            "ev_pct": 5.05,
            "kelly_stake": 500.0,  # Exceeds $300 cap -> should be capped at $300 (30u)
        }
        text = format_telegram_alert(opp)
        self.assertIn("Sport:</b> NBA", text)
        self.assertIn("Matchup:</b> Boston Celtics vs Dallas Mavericks", text)
        self.assertIn("Bet / Line:</b> Boston Celtics -4.5 (Spread)", text)
        self.assertIn("Odds:</b> -110 (1.91)", text)
        self.assertIn("Sportsbook:</b> DraftKings", text)
        self.assertIn("Fair Odds:</b>", text)
        self.assertIn("EV%:</b> +5.05%", text)
        self.assertIn("Recommended Stake:</b> 30u <i>(1u = 1% of bankroll)</i>", text)
        self.assertNotIn("($300.00)", text)

    def test_format_welcome_guide_content(self):
        guide = format_welcome_guide("Alex")
        self.assertIn("Welcome to Not Your Bookie Edge, Alex!", guide)
        self.assertIn("What is +EV (Expected Value)?", guide)
        self.assertIn("What is a 'Unit' (u)?", guide)
        self.assertIn("1 unit (1u) = 1% of your total betting bankroll.", guide)
        self.assertIn("What is an Arbitrage Alert", guide)
        self.assertIn("How to Read Alerts:", guide)
        self.assertIn("Pro-Tip:", guide)

    def test_check_and_welcome_new_members(self):
        with patch("worker._new_telegram_session") as mock_session_factory:
            mock_session = MagicMock()
            mock_session.get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "result": [
                        {
                            "update_id": 1001,
                            "message": {
                                "chat": {"id": 12345},
                                "new_chat_members": [
                                    {"id": 999, "first_name": "Jordan", "is_bot": False}
                                ],
                            },
                        }
                    ]
                },
            )
            mock_session.post.return_value = MagicMock(status_code=200)
            mock_session_factory.return_value = mock_session

            welcomed = check_and_welcome_new_members("fake_token", "12345")
            self.assertEqual(welcomed, 1)
            mock_session.post.assert_called_once()
            call_kwargs = mock_session.post.call_args[1]
            self.assertIn("Jordan", call_kwargs["json"]["text"])

    def test_format_telegram_arbitrage_alert(self):
        arb = {
            "sport": "NBA",
            "matchup": "Lakers vs Warriors",
            "market": "Moneyline",
            "total_stake": 500.0,
            "profit": 12.25,
            "roi_pct": 2.45,
            "locked_payout": 512.25,
            "legs": [
                {
                    "selection": "Lakers",
                    "sportsbook": "FanDuel",
                    "odds_decimal": 2.15,
                    "odds_american": 115,
                    "stake": 238.0,
                    "payout": 511.70,
                },
                {
                    "selection": "Warriors",
                    "sportsbook": "DraftKings",
                    "odds_decimal": 1.95,
                    "odds_american": -105,
                    "stake": 262.0,
                    "payout": 510.90,
                },
            ],
        }
        text = format_telegram_arbitrage_alert(arb)
        self.assertIn("Arbitrage Opportunity (Guaranteed Profit)", text)
        self.assertIn("Sport:</b> NBA", text)
        self.assertIn("Total Stake:</b> $500", text)
        self.assertIn("max $300 per bet", text)
        self.assertIn("Guaranteed Profit:</b> +$12.25 (ROI: +2.45%)", text)
        self.assertIn("Lakers</b> at <b>FanDuel", text)
        self.assertIn("Stake: <b>$238</b>", text)
        self.assertIn("Warriors</b> at <b>DraftKings", text)
        self.assertIn("Stake: <b>$262</b>", text)

    def test_send_telegram_message_success_and_failure(self):
        with patch("worker._new_telegram_session") as mock_session_factory:
            mock_session = MagicMock()
            mock_session.post.return_value = MagicMock(status_code=200)
            mock_session_factory.return_value = mock_session
            self.assertTrue(send_telegram_message("test_token", "test_chat", "Hello"))

        with patch("worker._new_telegram_session") as mock_session_factory:
            mock_session = MagicMock()
            mock_session.post.return_value = MagicMock(status_code=400, text="Bad Request")
            mock_session_factory.return_value = mock_session
            self.assertFalse(send_telegram_message("test_token", "test_chat", "Hello"))

        self.assertFalse(send_telegram_message("", "test_chat", "Hello"))
        self.assertFalse(send_telegram_message("test_token", "", "Hello"))

    def test_save_and_load_alert_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "alert_history.json")
            data = {"game_1": {"ev": 4.5, "timestamp": 123456.7}}
            save_alert_history(data, file_path)
            loaded = load_alert_history(file_path)
            self.assertEqual(loaded, data)

    def test_save_and_load_cached_odds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "cached_odds.json")
            quotes = [{"event_id": "1", "odds_decimal": 2.0}]
            opps = [{"event_id": "1", "ev_pct": 5.0}]
            arbs = [{"event_id": "1", "profit": 20.0}]
            now = datetime.now(timezone.utc)
            save_cached_odds(quotes, opps, arbs, ["NBA"], now, file_path)

            loaded = load_cached_odds(file_path)
            self.assertIsNotNone(loaded)
            self.assertEqual(len(loaded["quotes"]), 1)
            self.assertEqual(len(loaded["opportunities"]), 1)
            self.assertEqual(len(loaded["arbitrage"]), 1)
            self.assertEqual(loaded["sports"], ["NBA"])

    def test_run_worker_e2e_flow_with_ev_and_arbitrage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hist_file = os.path.join(tmpdir, "alert_history.json")
            cache_file = os.path.join(tmpdir, "cached_odds.json")

            future_kickoff = (
                datetime.now(timezone.utc) + timedelta(hours=3)
            ).isoformat()
            past_kickoff = (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat()

            mock_quotes = [
                {"event_id": "event_1", "kickoff": future_kickoff},
                {"event_id": "event_2", "kickoff": past_kickoff},
            ]

            mock_opps = [
                {
                    "event_id": "event_1",
                    "sport": "NBA",
                    "matchup": "Team A vs Team B",
                    "market": "Spread -2.5",
                    "selection": "Team A",
                    "line": -2.5,
                    "sportsbook": "FanDuel",
                    "odds_american": -110,
                    "odds_decimal": 1.91,
                    "fair_probability": 0.56,
                    "ev_pct": 4.5,
                    "kickoff": future_kickoff,
                },
            ]

            mock_arbs = [
                {
                    "sport": "NBA",
                    "event_id": "event_1",
                    "matchup": "Team A vs Team B",
                    "market": "Moneyline",
                    "kickoff": future_kickoff,
                    "total_stake": 500.0,
                    "implied_probability_sum": 0.9307,
                    "profit": 37.2,
                    "roi_pct": 7.44,
                    "locked_payout": 537.2,
                    "legs": [
                        {
                            "selection": "Team A",
                            "sportsbook": "FanDuel",
                            "odds_decimal": 2.2,
                            "stake": 244.0,
                            "payout": 536.8,
                        },
                        {
                            "selection": "Team B",
                            "sportsbook": "DraftKings",
                            "odds_decimal": 2.1,
                            "stake": 256.0,
                            "payout": 537.6,
                        },
                    ],
                },
                {
                    "sport": "NBA",
                    "event_id": "event_low",
                    "matchup": "Low ROI",
                    "market": "Moneyline",
                    "kickoff": future_kickoff,
                    "total_stake": 500.0,
                    "profit": 2.0,
                    "roi_pct": 0.4,  # Under 1.0% min ROI -> must be filtered
                    "legs": [
                        {"selection": "A", "sportsbook": "FanDuel", "odds_decimal": 2.0, "stake": 250.0},
                        {"selection": "B", "sportsbook": "DraftKings", "odds_decimal": 2.02, "stake": 250.0},
                    ],
                },
            ]

            with patch("worker.run_api") as mock_run_api, patch(
                "worker.scan_pinnacle_ev"
            ) as mock_scan, patch(
                "worker.find_arbitrage_opportunities"
            ) as mock_find_arb, patch(
                "worker.send_telegram_message"
            ) as mock_send:
                mock_run_api.return_value = {
                    "quotes": mock_quotes,
                    "errors": [],
                    "warnings": [],
                }
                mock_scan.return_value = {"opportunities": mock_opps}
                mock_find_arb.return_value = mock_arbs
                mock_send.return_value = True

                with patch.dict(
                    os.environ,
                    {
                        "PROPLINE_API_KEY": "test_api_key",
                        "TELEGRAM_BOT_TOKEN": "bot_token",
                        "TELEGRAM_CHAT_ID": "chat_id",
                    },
                ):
                    with patch("worker.in_peak_window", return_value=True):
                      result = run_worker(
                        sports=["NBA"],
                        min_ev=3.0,
                        min_arb_roi=1.0,
                        history_file=hist_file,
                        cache_file=cache_file,
                    )

            self.assertTrue(result["success"])
            self.assertEqual(result["eligible_opportunities"], 1)
            # Fixture arb is 7.4% ROI / $37.20 profit: clears the 1% ROI floor and $10 profit minimum.
            self.assertEqual(result["eligible_arbitrages"], 1)
            self.assertEqual(result["ev_alerts_sent"], 1)
            self.assertEqual(result["arb_alerts_sent"], 1)
            self.assertEqual(mock_send.call_count, 2)

    def test_dry_run_does_not_poison_alert_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hist_file = os.path.join(tmpdir, "alert_history.json")
            cache_file = os.path.join(tmpdir, "cached_odds.json")
            seed = {"sentinel": {"ev": 1.0, "timestamp": 1.0}}
            save_alert_history(seed, hist_file)

            future_kickoff = (
                datetime.now(timezone.utc) + timedelta(hours=3)
            ).isoformat()
            mock_opps = [
                {
                    "event_id": "dry-event",
                    "sport": "NBA",
                    "matchup": "Team A vs Team B",
                    "market": "Spread -2.5",
                    "selection": "Team A",
                    "line": -2.5,
                    "sportsbook": "FanDuel",
                    "odds_american": -110,
                    "odds_decimal": 1.91,
                    "fair_probability": 0.56,
                    "ev_pct": 4.5,
                    "kickoff": future_kickoff,
                }
            ]
            mock_arbs = [
                {
                    "sport": "NBA",
                    "event_id": "dry-event",
                    "matchup": "Team A vs Team B",
                    "market": "Moneyline",
                    "kickoff": future_kickoff,
                    "total_stake": 500.0,
                    "implied_probability_sum": 0.9307,
                    "profit": 37.2,
                    "roi_pct": 7.44,
                    "locked_payout": 537.2,
                    "legs": [
                        {
                            "selection": "Team A",
                            "sportsbook": "FanDuel",
                            "odds_decimal": 2.2,
                            "stake": 244.0,
                            "payout": 536.8,
                        },
                        {
                            "selection": "Team B",
                            "sportsbook": "DraftKings",
                            "odds_decimal": 2.1,
                            "stake": 256.0,
                            "payout": 537.6,
                        },
                    ],
                }
            ]

            with (
                patch("worker.run_api") as mock_run_api,
                patch("worker.scan_pinnacle_ev") as mock_scan,
                patch("worker.find_arbitrage_opportunities") as mock_find_arb,
                patch("worker.send_telegram_message") as mock_send,
            ):
                mock_run_api.return_value = {
                    "quotes": [{"event_id": "dry-event", "kickoff": future_kickoff}],
                    "errors": [],
                    "warnings": [],
                }
                mock_scan.return_value = {"opportunities": mock_opps}
                mock_find_arb.return_value = mock_arbs
                mock_send.return_value = True

                with patch.dict(
                    os.environ,
                    {
                        "PROPLINE_API_KEY": "test_api_key",
                        "TELEGRAM_BOT_TOKEN": "bot_token",
                        "TELEGRAM_CHAT_ID": "chat_id",
                    },
                ):
                    with (
                        patch("worker.in_peak_window", return_value=True),
                        redirect_stdout(io.StringIO()),
                    ):
                        result = run_worker(
                            sports=["NBA"],
                            min_ev=3.0,
                            min_arb_roi=1.0,
                            history_file=hist_file,
                            cache_file=cache_file,
                            dry_run=True,
                        )

            self.assertTrue(result["success"])
            self.assertEqual(result["ev_alerts_sent"], 1)
            self.assertEqual(result["arb_alerts_sent"], 1)
            mock_send.assert_not_called()
            self.assertEqual(load_alert_history(hist_file), seed)

    def test_get_api_keys_ordering_and_fallback(self):
        with patch.dict(
            os.environ,
            {
                "PROPLINE_API_KEY": "primary_key",
                "PROPLINE_API_KEY_BACKUP": "backup_key",
            },
            clear=True,
        ):
            keys = get_api_keys()
            self.assertEqual(keys, ["primary_key", "backup_key"])

    def test_fetch_odds_with_fallback_success_on_first_key(self):
        with patch("worker.run_api") as mock_run_api:
            mock_run_api.return_value = {
                "quotes": [{"odds_decimal": 2.0}],
                "errors": [],
                "warnings": [],
                "quota": {"X-Daily-Remaining": "500"},
            }
            res, used_key = fetch_odds_with_fallback(["NBA"], ["key_1", "key_2"])
            self.assertEqual(used_key, "key_1")
            self.assertEqual(mock_run_api.call_count, 1)

    def test_fetch_odds_with_fallback_rollover_on_429(self):
        with patch("worker.run_api") as mock_run_api:
            # First key returns 429 quota error, second key succeeds
            mock_run_api.side_effect = [
                {
                    "quotes": [],
                    "errors": ["NBA: PropLine rate or daily quota limit reached (HTTP 429)."],
                    "warnings": [],
                    "quota": {"X-Daily-Remaining": "0"},
                },
                {
                    "quotes": [{"odds_decimal": 2.1}],
                    "errors": [],
                    "warnings": [],
                    "quota": {"X-Daily-Remaining": "995"},
                },
            ]
            res, used_key = fetch_odds_with_fallback(["NBA"], ["key_1", "key_2"])
            self.assertEqual(used_key, "key_2")
            self.assertEqual(mock_run_api.call_count, 2)
            self.assertEqual(len(res["quotes"]), 1)

    def test_default_ev_floor_is_3_5_percent_with_no_upper_cap(self):
        self.assertEqual(DEFAULT_MIN_EV_THRESHOLD, 3.5)
        self.assertEqual(CONFIG.worker_min_ev_pct, 3.5)
        self.assertIsNone(DEFAULT_MAX_EV_THRESHOLD)
        self.assertIsNone(CONFIG.worker_max_ev_pct)

    def test_worker_ev_settings_allow_uncapped_environment(self):
        with patch.dict(
            os.environ,
            {"WORKER_MIN_EV_PCT": "4.25", "WORKER_MAX_EV_PCT": "none"},
            clear=False,
        ):
            self.assertEqual(worker_ev_settings(), (4.25, None))

    def test_telegram_ev_alert_escapes_feed_text(self):
        opportunity = {
            "sport": "NBA",
            "matchup": "<b>Home</b> & Away",
            "market": "Moneyline",
            "selection": "Home <script>",
            "sportsbook": "Book & Co",
            "odds_american": 120,
            "odds_decimal": 2.2,
            "fair_probability": 0.5,
            "ev_pct": 10.0,
            "kelly_stake": 10.0,
        }
        message = format_telegram_alert(opportunity)
        self.assertIn("&lt;b&gt;Home&lt;/b&gt; &amp; Away", message)
        self.assertIn("Home &lt;script&gt;", message)
        self.assertIn("Book &amp; Co", message)
        self.assertNotIn("<script>", message)
        self.assertNotIn("<b>Home</b>", message)

    def test_apply_arbitrage_size_limits_enforces_the_per_leg_maximum(self):
        # Lopsided stakes exceed the $300 per-leg rule; the helper scales the
        # total down without applying any profit cap.
        arb = {
            "total_stake": 1000,
            "legs": [
                {
                    "selection": "Home",
                    "sportsbook": "DraftKings",
                    "odds_decimal": 2.5,
                    "stake": 625,
                    "payout": 1562.5,
                },
                {
                    "selection": "Away",
                    "sportsbook": "FanDuel",
                    "odds_decimal": 1.7,
                    "stake": 375,
                    "payout": 637.5,
                },
            ],
        }
        self.assertTrue(
            apply_arbitrage_size_limits(
                arb,
                max_leg_stake=300.0,
                rounding_step=1,
            )
        )
        self.assertLess(arb["total_stake"], 1000)
        self.assertTrue(arb["stake_reduced"])
        self.assertGreater(arb["profit"], 0)
        self.assertLessEqual(max(leg["stake"] for leg in arb["legs"]), 300)
        payouts = [leg["payout"] for leg in arb["legs"]]
        self.assertGreaterEqual(min(payouts), arb["total_stake"])
        self.assertEqual(
            round(min(payouts) - arb["total_stake"], 2), arb["profit"]
        )

    def test_atomic_json_write_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            _atomic_write_json(path, {"ok": True})

            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(f"{path}.tmp"))
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"ok": True})


if __name__ == "__main__":
    unittest.main()
