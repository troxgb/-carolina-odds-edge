import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from propline_api import (
    APPROVED_BOOKMAKERS,
    EASTERN,
    _event_is_in_mode,
    american_to_decimal,
    get_rotating_api_keys,
    load_quota_state,
    parse_odds_payload,
    run_api,
    sport_markets,
)


class PropLineApiTests(unittest.TestCase):
    def test_north_carolina_bookmaker_allowlist_is_exact(self):
        self.assertEqual(
            APPROVED_BOOKMAKERS,
            {
                "draftkings": "DraftKings",
                "fanduel": "FanDuel",
                "betmgm": "BetMGM",
                "fanatics": "Fanatics",
                "thescorebet": "theScore Bet",
                "novig": "Novig",
                "prophetx": "ProphetX",
            },
        )

    def test_american_prices_and_invalid_values(self):
        self.assertAlmostEqual(american_to_decimal(-110), 1.9090909)
        self.assertEqual(american_to_decimal(150), 2.5)
        self.assertIsNone(american_to_decimal(0))
        self.assertIsNone(american_to_decimal("110"))

    def test_alternate_lines_and_unconfigured_props_are_dropped(self):
        events = [
            {
                "id": "event-lines",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "totals",
                                "line_type": "main",
                                "outcomes": [
                                    {"name": "Over", "price": -110, "point": 44.5},
                                    {"name": "Under", "price": -110, "point": 44.5},
                                ],
                            },
                            {
                                "key": "totals",
                                "line_type": "alternate",
                                "outcomes": [
                                    {"name": "Over", "price": -105, "point": 52.5},
                                    {"name": "Under", "price": -115, "point": 52.5},
                                ],
                            },
                            {
                                "key": "player_turnovers",
                                "line_type": "main",
                                "outcomes": [
                                    {"name": "Over", "price": -110, "point": 2.5},
                                    {"name": "Under", "price": -110, "point": 2.5},
                                ],
                            },
                        ],
                    }
                ],
            }
        ]

        quotes = parse_odds_payload(events, "NBA")

        self.assertEqual({quote["point"] for quote in quotes}, {44.5})
        self.assertFalse(any(quote["is_player_prop"] for quote in quotes))

    def test_approved_bookmaker_quotes_keep_reported_liquidity(self):
        events = [
            {
                "id": "event-draftkings",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": 125,
                                        "liquidity": 40.5,
                                        "liquidity_updated_at": "2026-11-13T10:00:00Z",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        quotes = parse_odds_payload(events, "NFL")

        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["sportsbook"], "DraftKings")
        self.assertEqual(quotes[0]["selection"], "Home")
        self.assertEqual(quotes[0]["odds_american"], 125)
        self.assertEqual(quotes[0]["price_format"], "american")
        self.assertEqual(quotes[0]["odds_decimal"], 2.25)
        self.assertEqual(quotes[0]["liquidity"], 40.5)
        self.assertEqual(
            quotes[0]["liquidity_updated_at"], "2026-11-13T10:00:00Z"
        )

    def test_schedule_filter_uses_current_eastern_time(self):
        now = datetime(2026, 11, 13, 16, 18, tzinfo=EASTERN)
        past_same_day = {
            "commence_time": "2026-11-13T21:17:00Z",
            "live": False,
        }
        future_same_day = {
            "commence_time": "2026-11-13T21:19:00Z",
            "live": False,
        }
        future_next_day = {
            "commence_time": "2026-11-14T15:00:00Z",
            "live": False,
        }

        self.assertFalse(_event_is_in_mode(past_same_day, "upcoming", now))
        self.assertTrue(_event_is_in_mode(future_same_day, "upcoming", now))
        self.assertTrue(_event_is_in_mode(future_next_day, "upcoming", now))
        self.assertFalse(_event_is_in_mode(future_same_day, "live", now))
        self.assertTrue(
            _event_is_in_mode(
                {"commence_time": "2026-11-13T20:00:00Z", "live": True},
                "live",
                now,
            )
        )
        self.assertFalse(
            _event_is_in_mode(
                {
                    "commence_time": "2026-11-13T20:00:00Z",
                    "live": True,
                    "completed": True,
                },
                "live",
                now,
            )
        )

    def test_parser_keeps_alternate_lines_separate(self):
        events = [
            {
                "id": "event-1",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "side": "home", "price": -110},
                                    {"name": "Away", "side": "away", "price": 100},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": -110, "point": 44.5},
                                    {"name": "Under", "price": -110, "point": 44.5},
                                ],
                            },
                        ],
                    }
                ],
            }
        ]

        quotes = parse_odds_payload(events, "NFL")

        self.assertEqual(len(quotes), 4)
        self.assertEqual(quotes[0]["market_key"], "h2h")
        self.assertEqual(quotes[2]["market_key"], "totals:44.5")
        self.assertEqual(quotes[2]["expected_outcomes"], ["Over", "Under"])
        self.assertAlmostEqual(quotes[0]["odds_decimal"], 1.9090909)

    def test_parser_keeps_signed_spread_sides_for_the_same_team(self):
        # A book can list both Home +4.5 and Home -4.5 as alternate lines. They
        # are different wagers and must not collapse into one quote during
        # deduplication, or the surviving side can pair with the wrong number.
        events = [
            {
                "id": "event-signed-spread",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": -110,
                                        "point": 4.5,
                                    },
                                    {
                                        "name": "Away",
                                        "side": "away",
                                        "price": -110,
                                        "point": -4.5,
                                    },
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": 320,
                                        "point": -4.5,
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        quotes = parse_odds_payload(events, "NFL")

        self.assertEqual(len(quotes), 3)
        home_points = sorted(
            float(item["point"])
            for item in quotes
            if item["selection"] == "Home"
        )
        self.assertEqual(home_points, [-4.5, 4.5])
        self.assertTrue(
            all(item["market_key"] == "spreads:4.5" for item in quotes)
        )

    def test_parser_keeps_game_and_team_totals_separate(self):
        events = [
            {
                "id": "event-totals",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": side, "price": -110, "point": 110.5}
                                    for side in ("Over", "Under")
                                ],
                            },
                            {
                                "key": "totals",
                                "team": "Home",
                                "outcomes": [
                                    {"name": side, "price": price, "point": 110.5}
                                    for side, price in (("Over", -200), ("Under", 170))
                                ],
                            },
                            {
                                "key": "totals",
                                "team": "Away",
                                "outcomes": [
                                    {"name": side, "price": price, "point": 110.5}
                                    for side, price in (("Over", 150), ("Under", -170))
                                ],
                            },
                        ],
                    }
                ],
            }
        ]
        quotes = parse_odds_payload(events, "NBA")

        totals = {quote["market_key"]: quote for quote in quotes}
        self.assertEqual(
            set(totals),
            {"totals:110.5", "totals:110.5:team:home", "totals:110.5:team:away"},
        )
        self.assertEqual(
            totals["totals:110.5:team:home"]["market"], "Total 110.5 (Home team)"
        )
        self.assertEqual(
            totals["totals:110.5:team:away"]["market"], "Total 110.5 (Away team)"
        )

    def test_parser_uses_freshest_duplicate_market_row(self):
        events = [
            {
                "id": "event-freshness",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "spreads",
                                "last_update": "2026-11-13T10:00:00Z",
                                "outcomes": [
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": 3300,
                                        "point": -3.5,
                                        "last_seen_at": "2026-11-13T10:00:00Z",
                                    },
                                    {
                                        "name": "Away",
                                        "side": "away",
                                        "price": 3300,
                                        "point": 3.5,
                                        "last_seen_at": "2026-11-13T10:00:00Z",
                                    },
                                ],
                            },
                            {
                                "key": "spreads",
                                "last_update": "2026-11-13T10:05:00Z",
                                "outcomes": [
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": -110,
                                        "point": -3.5,
                                        "last_seen_at": "2026-11-13T10:05:00Z",
                                    },
                                    {
                                        "name": "Away",
                                        "side": "away",
                                        "price": -110,
                                        "point": 3.5,
                                        "last_seen_at": "2026-11-13T10:05:00Z",
                                    },
                                ],
                            },
                        ],
                    }
                ],
            }
        ]

        quotes = parse_odds_payload(events, "NBA")

        self.assertEqual(len(quotes), 2)
        self.assertTrue(all(quote["odds_decimal"] < 2 for quote in quotes))
        self.assertTrue(
            all(quote["last_update"] == "2026-11-13T10:05:00Z" for quote in quotes)
        )

    def test_parser_drops_outcome_missing_from_latest_market_delivery(self):
        events = [
            {
                "id": "event-withdrawn",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-11-13T10:05:00Z",
                                "outcomes": [
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": 110,
                                        "last_seen_at": "2026-11-13T10:00:00Z",
                                    },
                                    {
                                        "name": "Away",
                                        "side": "away",
                                        "price": -120,
                                        "last_seen_at": "2026-11-13T10:05:00Z",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        quotes = parse_odds_payload(events, "NBA")

        self.assertEqual([quote["selection"] for quote in quotes], ["Away"])

    def test_parser_keeps_only_approved_books_and_uses_team_side(self):
        events = [
            {
                "id": "event-2",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home alias", "side": "home", "price": 120},
                                    {"name": "Away alias", "side": "away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "bovada",
                        "title": "Bovada",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "betrivers",
                        "title": "BetRivers",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "thescorebet",
                        "title": "theScore Bet",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "novig",
                        "title": "Novig",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "prophetx",
                        "title": "ProphetX",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "unapproved_provider",
                        "title": "Unapproved Provider",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
        for bookmaker_key, title in (
            ("fanduel", "FanDuel"),
            ("betmgm", "BetMGM"),
            ("fanatics", "Fanatics"),
        ):
            events[0]["bookmakers"].append(
                {
                    "key": bookmaker_key,
                    "title": title,
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Home", "price": 120},
                                {"name": "Away", "price": -130},
                            ],
                        }
                    ],
                }
            )

        quotes = parse_odds_payload(events, "NBA")

        self.assertEqual(APPROVED_BOOKMAKERS["draftkings"], "DraftKings")
        self.assertEqual(
            {quote["sportsbook"] for quote in quotes},
            {
                "DraftKings",
                "FanDuel",
                "BetMGM",
                "Fanatics",
                "theScore Bet",
                "Novig",
                "ProphetX",
            },
        )
        self.assertEqual({quote["selection"] for quote in quotes}, {"Home", "Away"})

    def test_parser_includes_pinnacle_without_mixing_three_way_books(self):
        events = [
            {
                "id": "event-pinnacle",
                "home_team": "Home",
                "away_team": "Away",
                "commence_time": "2026-11-14T15:00:00Z",
                "bookmakers": [
                    {
                        "key": "pinnacle",
                        "title": "Pinnacle",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-11-13T10:00:00Z",
                                "outcomes": [
                                    {"name": "Home", "price": -110},
                                    {"name": "Away", "price": -110},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 140},
                                    {"name": "Away", "price": 130},
                                    {"name": "Draw", "price": 220},
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

        quotes = parse_odds_payload(events, "Soccer")
        pinnacle_quotes = [
            item for item in quotes if item["bookmaker_key"] == "pinnacle"
        ]
        draftkings_quotes = [
            item for item in quotes if item["bookmaker_key"] == "draftkings"
        ]

        self.assertEqual(len(pinnacle_quotes), 2)
        self.assertTrue(
            all(item["expected_outcomes"] == ["Home", "Away"] for item in pinnacle_quotes)
        )
        self.assertTrue(
            all("Draw" in item["expected_outcomes"] for item in draftkings_quotes)
        )
        self.assertEqual({item["odds_american"] for item in pinnacle_quotes}, {-110})

    def test_bulk_scan_uses_one_request_for_selected_sport(self):
        response = Mock()
        response.status_code = 200
        response.headers = {
            "X-Daily-Remaining": "997",
            "X-Daily-Limit": "1000",
        }
        response.json.return_value = [
            {
                "id": "live-game",
                "home_team": "Home",
                "away_team": "Away",
                "live": True,
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        session = Mock()
        session.get.return_value = response
        with patch("propline_api.requests.Session", return_value=session):
            result = run_api(["NBA"], "live", "test-key")

        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["quota"]["X-Daily-Remaining"], "997")
        self.assertEqual(len(result["quotes"]), 2)
        session.get.assert_called_once()
        self.assertEqual(
            session.get.call_args.kwargs["params"],
            {
                "markets": (
                    "h2h,spreads,totals,"
                    "player_points,player_rebounds,player_assists"
                ),
                "bookmakers": ",".join(
                    [*APPROVED_BOOKMAKERS, "pinnacle"]
                ),
                "max_age": 60,
            },
        )
        self.assertEqual(
            session.get.call_args.kwargs["headers"], {"X-API-Key": "test-key"}
        )
        session.close.assert_called_once()

    def test_transient_tls_handshake_gets_two_retries(self):
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = []
        initial_session = Mock()
        initial_session.get.side_effect = requests.exceptions.SSLError(
            "transient TLS failure"
        )
        retry_session = Mock()
        retry_session.get.side_effect = requests.exceptions.SSLError(
            "second transient TLS failure"
        )
        final_session = Mock()
        final_session.get.return_value = response

        with (
            patch(
                "propline_api.requests.Session",
                side_effect=[initial_session, retry_session, final_session],
            ) as make_session,
            patch("propline_api.time.sleep") as sleep,
        ):
            result = run_api(["NBA"], "live", "test-key")

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(make_session.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        initial_session.close.assert_called_once()
        retry_session.close.assert_called_once()
        retry_session.get.assert_called_once()
        final_session.get.assert_called_once()
        final_session.close.assert_called_once()

    def test_read_timeout_retries_once_with_a_fresh_session(self):
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = []
        initial_session = Mock()
        initial_session.get.side_effect = requests.exceptions.ReadTimeout(
            "read timed out"
        )
        retry_session = Mock()
        retry_session.get.return_value = response

        with (
            patch(
                "propline_api.requests.Session",
                side_effect=[initial_session, retry_session],
            ) as make_session,
            patch("propline_api.time.sleep") as sleep,
        ):
            result = run_api(["NCAAF"], "live", "test-key")

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(make_session.call_count, 2)
        self.assertEqual(sleep.call_args.args, (0.5,))
        initial_session.get.assert_called_once()
        retry_session.get.assert_called_once()
        initial_session.close.assert_called_once()
        retry_session.close.assert_called_once()

    def test_read_timeout_reports_after_retry_exhausted(self):
        initial_session = Mock()
        retry_session = Mock()
        initial_session.get.side_effect = requests.exceptions.ReadTimeout(
            "first read timed out"
        )
        retry_session.get.side_effect = requests.exceptions.ReadTimeout(
            "second read timed out"
        )

        with (
            patch(
                "propline_api.requests.Session",
                side_effect=[initial_session, retry_session],
            ),
            patch("propline_api.time.sleep"),
        ):
            result = run_api(["NCAAF"], "live", "test-key")

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("request timed out after 2 attempt(s)", result["errors"][0])
        self.assertIn("NCAAF", result["errors"][0])
        initial_session.close.assert_called_once()
        retry_session.close.assert_called_once()

    def test_batch_continues_to_next_sport_after_tls_failure(self):
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = []
        first_session = Mock()
        second_session = Mock()
        third_session = Mock()
        fourth_session = Mock()
        fifth_session = Mock()
        sixth_session = Mock()
        first_session.get.side_effect = requests.exceptions.SSLError(
            "transient TLS failure"
        )
        second_session.get.side_effect = requests.exceptions.SSLError(
            "retry TLS failure"
        )
        third_session.get.side_effect = requests.exceptions.SSLError(
            "final TLS failure"
        )
        fourth_session.get.side_effect = requests.exceptions.SSLError(
            "fourth TLS failure"
        )
        fifth_session.get.side_effect = requests.exceptions.SSLError(
            "fifth TLS failure"
        )
        sixth_session.get.return_value = response

        with (
            patch(
                "propline_api.requests.Session",
                side_effect=[
                    first_session,
                    second_session,
                    third_session,
                    fourth_session,
                    fifth_session,
                    sixth_session,
                ],
            ) as make_session,
            patch("propline_api.time.sleep"),
        ):
            result = run_api(["NBA", "NHL"], "live", "test-key")

        self.assertEqual(result["request_count"], 2)
        self.assertEqual(make_session.call_count, 6)
        self.assertIn("NBA: TLS handshake failed after 5 attempts", result["errors"][0])
        self.assertEqual(sixth_session.get.call_count, 1)
        first_session.close.assert_called_once()
        second_session.close.assert_called_once()
        third_session.close.assert_called_once()
        fourth_session.close.assert_called_once()
        fifth_session.close.assert_called_once()
        sixth_session.close.assert_called_once()

    def test_certificate_validation_error_is_not_retried(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.SSLError(
            "certificate verify failed"
        )

        with (
            patch("propline_api.requests.Session", return_value=session) as make_session,
            patch("propline_api.time.sleep") as sleep,
        ):
            result = run_api(["NBA"], "live", "test-key")

        self.assertEqual(result["request_count"], 1)
        self.assertIn("TLS certificate validation failed", result["errors"][0])
        session.get.assert_called_once()
        make_session.assert_called_once()
        sleep.assert_not_called()
        session.close.assert_called_once()

    def test_missing_key_does_not_make_a_request(self):
        with patch("propline_api.requests.Session") as make_session:
            result = run_api(["NBA"], api_key="")

        self.assertEqual(result["request_count"], 0)
        self.assertTrue(result["errors"])
        make_session.assert_not_called()

    def test_bulk_markets_include_player_props_per_sport(self):
        self.assertEqual(
            sport_markets("NBA"),
            "h2h,spreads,totals,player_points,player_rebounds,player_assists",
        )
        self.assertEqual(
            sport_markets("NCAAB"),
            "h2h,spreads,totals,player_points,player_rebounds,player_assists",
        )
        self.assertEqual(
            sport_markets("NFL"),
            "h2h,spreads,totals,player_pass_yds,player_rush_yds,"
            "player_reception_yds,player_pass_tds,player_receptions",
        )
        self.assertEqual(
            sport_markets("NCAAF"),
            "h2h,spreads,totals,player_pass_yds,player_rush_yds,"
            "player_reception_yds",
        )
        # Non-prop sports keep the original three markets.
        self.assertEqual(sport_markets("MLB"), "h2h,spreads,totals")

    def test_dual_keys_rotate_at_noon_utc(self):
        with patch.dict(
            os.environ,
            {
                "PROPLINE_API_KEY_1": "key_one",
                "PROPLINE_API_KEY_2": "key_two",
            },
            clear=True,
        ):
            morning = datetime(2026, 1, 1, 11, 59, tzinfo=timezone.utc)
            afternoon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
            self.assertEqual(
                get_rotating_api_keys(morning), ["key_one", "key_two"]
            )
            self.assertEqual(
                get_rotating_api_keys(afternoon), ["key_two", "key_one"]
            )

    def test_low_primary_quota_fails_over_to_secondary_key(self):
        low_response = Mock()
        low_response.status_code = 200
        low_response.headers = {"X-Daily-Remaining": "5"}
        low_response.json.return_value = []

        healthy_response = Mock()
        healthy_response.status_code = 200
        healthy_response.headers = {"X-Daily-Remaining": "990"}
        healthy_response.json.return_value = []

        session = Mock()
        session.get.side_effect = [low_response, healthy_response]

        with tempfile.TemporaryDirectory() as tmpdir:
            quota_file = os.path.join(tmpdir, "quota_state.json")
            with patch("propline_api.requests.Session", return_value=session):
                result = run_api(
                    ["NBA"],
                    "live",
                    api_keys=["primary_key", "secondary_key"],
                    quota_state_file=quota_file,
                    failover_threshold=15,
                )

            used_keys = [
                call.kwargs["headers"]["X-API-Key"]
                for call in session.get.call_args_list
            ]
            self.assertEqual(used_keys, ["primary_key", "secondary_key"])
            self.assertTrue(result["failover_used"])
            self.assertEqual(result["request_count"], 2)

            state = load_quota_state(quota_file)
            self.assertEqual(len(state["keys"]), 2)

    def test_rate_limited_primary_fails_over_to_secondary_key(self):
        limited_response = Mock()
        limited_response.status_code = 429
        limited_response.headers = {
            "X-Daily-Remaining": "0",
            "Retry-After": "30",
        }

        healthy_response = Mock()
        healthy_response.status_code = 200
        healthy_response.headers = {"X-Daily-Remaining": "995"}
        healthy_response.json.return_value = []

        session = Mock()
        session.get.side_effect = [limited_response, healthy_response]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("propline_api.requests.Session", return_value=session):
                result = run_api(
                    ["NBA"],
                    "live",
                    api_keys=["primary_key", "secondary_key"],
                    quota_state_file=os.path.join(tmpdir, "quota_state.json"),
                )

        self.assertTrue(result["failover_used"])
        used_keys = [
            call.kwargs["headers"]["X-API-Key"]
            for call in session.get.call_args_list
        ]
        self.assertEqual(used_keys, ["primary_key", "secondary_key"])

    def test_rate_limit_is_reported_without_retrying_requests(self):
        response = Mock()
        response.status_code = 429
        response.headers = {
            "Retry-After": "30",
            "X-Daily-Remaining": "0",
            "X-Daily-Limit": "1000",
        }

        session = Mock()
        session.get.return_value = response
        with patch("propline_api.requests.Session", return_value=session):
            result = run_api(["NBA", "NHL"], "live", "test-key")

        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["quota"]["X-Daily-Remaining"], "0")
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue(all("No automatic retry" in error for error in result["errors"]))
        self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
