from dataclasses import dataclass


@dataclass(frozen=True)
class ScannerConfig:
    pinnacle_bookmaker_key: str = "pinnacle"
    pinnacle_bookmaker_name: str = "Pinnacle"
    devig_method: str = "power"
    # Interactive board floor. The cron worker uses worker_min_ev_pct below.
    min_ev_pct: float = 2.0
    min_liquidity_dollars: float = 25.0
    verify_ev_pct: float = 15.0
    # Scanner staleness: PropLine last_update values are usually 2-5 minutes
    # old (only ~3% of quotes are <=60s), so a 60s window discarded ~97% of
    # candidates. In an A/B test 300s surfaced two 3.5%+ EV alerts that 60s
    # dropped entirely. Kept separate from the API-side max_age below.
    max_quote_age_seconds: int = 300
    # API-side max_age stays at 60s: raising it to 300s added 42% more quotes
    # (16k -> 25k, +55MB peak RSS) but produced no additional opportunities.
    api_max_age_seconds: int = 60
    # Stop alerting this many minutes before the feed's kickoff time. PropLine's
    # start times are sometimes 30 minutes late (e.g. Florida Atlantic vs South
    # Florida: feed said 3:00 PM ET, real kickoff 2:30 PM), which let alerts fire
    # after a game was live. Alerts this close to kickoff are also hard to act
    # on, so the buffer costs little.
    prestart_guard_minutes: int = 30
    # The free tier returns every alternate/milestone line, which made up ~94%
    # of each response (one NCAAF call was 41 MB / 83k quotes). Keeping only
    # "main" lines cut a scan from 4 minutes to ~15 seconds on the 1 GB VPS.
    # Set to False to also scan alternate lines (needs a much larger host).
    main_lines_only: bool = True
    # Order-book depth is required to model the average price a bet can
    # actually fill at. PropLine returns it only for exchange books; on a full
    # NBA response it adds ~1.7% to the payload (no extra quota).
    include_depth: bool = True
    # Size used when measuring executable EV. PropLine's top-level `liquidity`
    # is the amount available at the quoted price, and `depth` holds the next
    # levels; a bet larger than the top level fills some of it at worse odds.
    executable_odds_probe_stake: float = 100.0
    include_book_ids: bool = False
    include_links: bool = False
    max_pinnacle_overround: float = 1.06
    bankroll: float = 1000.0
    kelly_fraction: float = 0.25
    max_bet_pct: float = 0.01
    max_event_pct: float = 0.03
    enable_line_adjusted: bool = False
    max_line_diff: float = 1.5
    line_adjusted_kelly_fraction: float | None = None
    # Keyed by (sport, "spreads" | "totals"). A missing entry disables the
    # distribution path for that sport and market. Values are starting points;
    # refine them with calibrate_sd.py.
    line_adjusted_stddev_by_sport: tuple[tuple[tuple[str, str], float], ...] = (
        (("NFL", "spreads"), 13.0),
        (("NFL", "totals"), 13.0),
        (("NCAAF", "spreads"), 17.0),
        (("NCAAF", "totals"), 17.0),
        (("NBA", "spreads"), 15.0),
        (("NBA", "totals"), 15.0),
        (("NCAAB", "spreads"), 14.0),
        (("NCAAB", "totals"), 14.0),
        (("MLB", "spreads"), 2.8),
        (("MLB", "totals"), 2.8),
        (("NHL", "spreads"), 1.8),
        (("NHL", "totals"), 1.8),
    )
    football_key_number_probability_boosts: tuple[tuple[int, float], ...] = (
        (3, 0.04),
        (7, 0.025),
    )
    api_timeout_seconds: int = 10
    api_timeout_retries: int = 1
    tls_retries: int = 4
    api_max_workers: int = 1
    arbitrage_rounding_step: int = 10
    max_close_lag_seconds: int = 900
    verify_stake_multiplier: float = 0.0
    liquidity_stake_fraction: float = 0.5
    book_fee_pct: tuple[tuple[str, float], ...] = (
        ("DraftKings", 0.0),
        ("FanDuel", 0.0),
        # ProphetX charges 2% of NET WINNINGS on pre-game straight contracts.
        # net_decimal_odds() models exactly that (fee on profit, not on stake).
        # Novig charges $0 on pre-game straights; its 0.06 x P x (1-P) per
        # contract taker fee only applies to live/futures trades, which this
        # scanner never requests (mode="upcoming").
        ("ProphetX", 0.02),
        ("Novig", 0.0),
    )
    # Peer-to-peer exchanges whose fill size must be verified before alerting.
    p2p_exchange_bookmaker_keys: tuple[str, ...] = ("prophetx", "novig")
    # Reject an exchange alert when the required stake exceeds this share of
    # the size available at the quoted price (headroom for race conditions).
    liquidity_buffer_multiplier: float = 0.85

    # --- Automated cron worker settings -------------------------------------
    # Every edge at or above this floor is alerted. There is intentionally no
    # upper cap: large edges remain visible and alertable (the UI still adds a
    # "verify" flag above verify_ev_pct).
    worker_min_ev_pct: float = 3.5
    worker_max_ev_pct: float | None = None
    # Arbitrage alerts are shown only when the rounded stakes still guarantee
    # at least this much profit. This is a floor, not a cap.
    min_arb_guaranteed_profit_dollars: float = 10.0
    # Maximum total stake (both legs) per arbitrage card. Protects bankroll
    # early on; the sizer scales down to fit.
    max_arb_total_stake_dollars: float = 300.0
    # NCAAB is the noisiest sport; use a stricter liquidity floor for it.
    ncaab_min_liquidity_dollars: float = 100.0

    # --- PropLine quota management ------------------------------------------
    quota_state_file: str = "quota_state.json"
    quota_failover_threshold: int = 15

    # --- Player props (Arbitrage only; never +EV) ---------------------------
    # Keyed by PropLine's exact sport key. Only these markets are appended to
    # the bulk odds request, and the scanner excludes them from +EV/CLV.
    player_prop_markets_by_sport: tuple[
        tuple[str, tuple[str, ...]], ...
    ] = (
        (
            "basketball_nba",
            ("player_points", "player_rebounds", "player_assists"),
        ),
        (
            "basketball_wnba",
            ("player_points", "player_rebounds", "player_assists"),
        ),
        (
            "basketball_ncaab",
            ("player_points", "player_rebounds", "player_assists"),
        ),
        (
            "football_nfl",
            (
                "player_pass_yds",
                "player_rush_yds",
                "player_reception_yds",
                "player_pass_tds",
                "player_receptions",
            ),
        ),
        (
            "football_ncaaf",
            ("player_pass_yds", "player_rush_yds", "player_reception_yds"),
        ),
    )

    @property
    def effective_line_adjusted_kelly_fraction(self) -> float:
        if self.line_adjusted_kelly_fraction is not None:
            return self.line_adjusted_kelly_fraction
        return self.kelly_fraction / 2

    def player_prop_markets_for_sport(self, sport_key: str) -> tuple[str, ...]:
        """Return the configured player prop market keys for a PropLine sport key."""
        for configured_sport, markets in self.player_prop_markets_by_sport:
            if configured_sport.casefold() == str(sport_key).casefold():
                return markets
        return ()


CONFIG = ScannerConfig()
DEVIG_METHODS = frozenset({"power", "shin", "multiplicative"})
