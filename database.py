import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pandas as pd

from scanner_config import CONFIG
from scanner_logic import (
    estimate_line_adjusted_probability,
    expected_value_with_push_pct,
    is_player_prop_market_key,
    is_player_prop_quote,
)

DB_PATH = os.getenv("BETS_DB_PATH", os.path.join("data", "bets.db"))


def _utc_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _closing_window(commence_time: str) -> tuple[str, str]:
    """Pinnacle snapshots usable as a close: in [kickoff - lag, kickoff]."""
    kickoff = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    earliest = kickoff - timedelta(seconds=CONFIG.max_close_lag_seconds)
    return earliest.astimezone(timezone.utc).isoformat(), kickoff.astimezone(
        timezone.utc
    ).isoformat()


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    existing = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for column, definition in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                mode TEXT,
                sport TEXT,
                matchup TEXT,
                market TEXT,
                selection TEXT,
                sportsbook TEXT,
                odds_american INTEGER,
                odds_decimal REAL,
                fair_prob_at_bet REAL,
                expected_ev_pct REAL,
                stake REAL,
                closing_odds_decimal REAL,
                clv_pct REAL,
                status TEXT,
                profit_loss REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pinnacle_market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                sport TEXT NOT NULL,
                event_id TEXT NOT NULL,
                market_key TEXT NOT NULL,
                period TEXT NOT NULL,
                team TEXT NOT NULL,
                selection TEXT NOT NULL,
                line REAL,
                kickoff TEXT,
                fair_probability REAL NOT NULL,
                pinnacle_american INTEGER,
                pinnacle_decimal REAL NOT NULL,
                overround REAL NOT NULL,
                quote_time TEXT,
                UNIQUE (
                    scan_id, sport, event_id, market_key, period, team,
                    selection, line
                )
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ev_opportunity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                sport TEXT NOT NULL,
                event_id TEXT NOT NULL,
                matchup TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                market TEXT NOT NULL,
                market_key TEXT NOT NULL,
                period TEXT NOT NULL,
                team TEXT NOT NULL,
                selection TEXT NOT NULL,
                sportsbook TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                line REAL,
                odds_american INTEGER,
                odds_decimal REAL NOT NULL,
                fair_probability REAL NOT NULL,
                ev_pct REAL NOT NULL,
                kelly_stake REAL NOT NULL,
                pinnacle_american_this_side INTEGER,
                pinnacle_american_other_side INTEGER,
                pinnacle_overround REAL NOT NULL,
                pinnacle_quote_time TEXT,
                book_quote_time TEXT,
                flags TEXT NOT NULL,
                pricing_method TEXT NOT NULL DEFAULT 'exact',
                line_adjustment_method TEXT NOT NULL DEFAULT '',
                pinnacle_line REAL,
                pinnacle_lines_used TEXT NOT NULL DEFAULT '',
                win_probability REAL,
                lose_probability REAL,
                push_probability REAL,
                closing_fair_probability REAL,
                closing_quote_time TEXT,
                clv_pct REAL,
                closing_win_probability REAL,
                closing_lose_probability REAL,
                closing_push_probability REAL,
                finalized_at TEXT,
                UNIQUE (scan_id, sport, event_id, market_key, period, team, selection, sportsbook)
            )
            """
        )
        _ensure_columns(
            conn,
            "bets",
            {"clv_basis": "TEXT NOT NULL DEFAULT 'vigged_price'"},
        )
        _ensure_columns(
            conn,
            "ev_opportunity_log",
            {
                "pricing_method": "TEXT NOT NULL DEFAULT 'exact'",
                "line_adjustment_method": "TEXT NOT NULL DEFAULT ''",
                "pinnacle_line": "REAL",
                "pinnacle_lines_used": "TEXT NOT NULL DEFAULT ''",
                "win_probability": "REAL",
                "lose_probability": "REAL",
                "push_probability": "REAL",
                "closing_win_probability": "REAL",
                "closing_lose_probability": "REAL",
                "closing_push_probability": "REAL",
            },
        )
        # Durable record of alerts that were actually dispatched. Unlike
        # alert_history.json (48h dedupe cache), this survives long enough to
        # backtest alert timing and EV floors across weeks.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                et_band TEXT NOT NULL,
                kind TEXT NOT NULL,
                alert_key TEXT NOT NULL,
                sport TEXT,
                event_id TEXT,
                market TEXT,
                market_key TEXT,
                selection TEXT,
                line REAL,
                bookmaker_key TEXT,
                sportsbook TEXT,
                odds_american INTEGER,
                ev_pct REAL,
                executable_ev_pct REAL,
                roi_pct REAL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alert_ledger_ts
            ON alert_ledger (ts_utc, kind)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pinnacle_snapshots_close
            ON pinnacle_market_snapshots (
                sport, event_id, market_key, period, team, selection, quote_time
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ev_opportunity_pending_close
            ON ev_opportunity_log (commence_time, closing_fair_probability)
            """
        )


def log_bets(bets_data: list[dict]) -> None:
    if not bets_data:
        raise ValueError("At least one bet is required.")
    init_db()
    timestamp = _now_utc_iso()
    rows = [
        (
            timestamp,
            bet_data.get("mode"),
            bet_data.get("sport"),
            bet_data.get("matchup"),
            bet_data.get("market"),
            bet_data.get("selection"),
            bet_data.get("sportsbook"),
            bet_data.get("odds_american"),
            bet_data.get("odds_decimal"),
            bet_data.get("fair_prob_at_bet"),
            bet_data.get("expected_ev_pct"),
            bet_data.get("stake"),
        )
        for bet_data in bets_data
    ]
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.executemany(
            """
            INSERT INTO bets (
                timestamp, mode, sport, matchup, market, selection, sportsbook,
                odds_american, odds_decimal, fair_prob_at_bet, expected_ev_pct,
                stake, status, profit_loss, clv_basis
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', 0.0, 'novig')
            """,
            rows,
        )


def log_bet(bet_data: dict) -> None:
    log_bets([bet_data])


def log_alerts(entries: list[dict]) -> None:
    """Persist successfully dispatched alerts in one small transaction.

    Called at most once per worker cycle with a handful of rows, so it never
    grows with the quote count and adds no per-quote memory overhead.
    """
    if not entries:
        return
    init_db()
    ts = _now_utc_iso()
    rows = [
        (
            entry.get("ts_utc") or ts,
            entry.get("et_band", "off"),
            entry.get("kind", "ev"),
            entry.get("alert_key", ""),
            entry.get("sport", ""),
            entry.get("event_id", ""),
            entry.get("market", ""),
            entry.get("market_key", ""),
            entry.get("selection", ""),
            entry.get("line"),
            entry.get("bookmaker_key", ""),
            entry.get("sportsbook", ""),
            entry.get("odds_american"),
            entry.get("ev_pct"),
            entry.get("executable_ev_pct"),
            entry.get("roi_pct"),
        )
        for entry in entries
    ]
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.executemany(
            """
            INSERT INTO alert_ledger (
                ts_utc, et_band, kind, alert_key, sport, event_id, market,
                market_key, selection, line, bookmaker_key, sportsbook,
                odds_american, ev_pct, executable_ev_pct, roi_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def load_bets() -> pd.DataFrame:
    init_db()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return pd.read_sql_query("SELECT * FROM bets", conn)


def update_bet_result(
    bet_id: int,
    status: str,
    closing_odds: float | None,
    profit_loss: float,
    clv_pct: float | None,
) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            """
            UPDATE bets
            SET status = ?, closing_odds_decimal = ?, profit_loss = ?, clv_pct = ?,
                clv_basis = 'novig'
            WHERE id = ?
            """,
            (status, closing_odds, profit_loss, clv_pct, bet_id),
        )


def log_scan(
    scan_id: str,
    observed_at: str,
    opportunities: list[dict],
    pinnacle_snapshots: list[dict],
) -> None:
    init_db()
    # Player props are arbitrage-only. Never persist them as Pinnacle +EV or
    # CLV snapshots, regardless of what the caller passes in.
    opportunities = [
        row for row in opportunities if not is_player_prop_quote(row)
    ]
    pinnacle_snapshots = [
        snapshot
        for snapshot in pinnacle_snapshots
        if not is_player_prop_market_key(snapshot.get("market_key"))
    ]
    observed_utc = _utc_iso(observed_at) or _now_utc_iso()
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO pinnacle_market_snapshots (
                scan_id, observed_at, sport, event_id, market_key, period, team,
                selection, line, kickoff, fair_probability, pinnacle_american,
                pinnacle_decimal, overround, quote_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    scan_id,
                    observed_utc,
                    snapshot.get("sport", ""),
                    snapshot.get("event_id", ""),
                    snapshot.get("market_key", ""),
                    snapshot.get("period", ""),
                    snapshot.get("team", ""),
                    snapshot.get("selection", ""),
                    snapshot.get("point"),
                    _utc_iso(snapshot.get("kickoff")),
                    snapshot["fair_probability"],
                    snapshot.get("pinnacle_american"),
                    snapshot["pinnacle_decimal"],
                    snapshot["overround"],
                    _utc_iso(snapshot.get("quote_time")),
                )
                for snapshot in pinnacle_snapshots
            ],
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO ev_opportunity_log (
                scan_id, observed_at, sport, event_id, matchup, commence_time,
                market, market_key, period, team, selection, sportsbook,
                bookmaker_key, line, odds_american, odds_decimal,
                fair_probability, ev_pct, kelly_stake,
                pinnacle_american_this_side, pinnacle_american_other_side,
                pinnacle_overround, pinnacle_quote_time, book_quote_time, flags,
                pricing_method, line_adjustment_method, pinnacle_line,
                pinnacle_lines_used, win_probability, lose_probability,
                push_probability
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                (
                    scan_id,
                    observed_utc,
                    row.get("sport", ""),
                    row.get("event_id", ""),
                    row.get("matchup", ""),
                    _utc_iso(row.get("kickoff")),
                    row.get("market", ""),
                    row.get("market_key", ""),
                    row.get("period", ""),
                    row.get("team") or "",
                    row.get("selection", ""),
                    row.get("sportsbook", ""),
                    row.get("bookmaker_key", ""),
                    row.get("line"),
                    row.get("odds_american"),
                    row["odds_decimal"],
                    row["fair_probability"],
                    row["ev_pct"],
                    row.get("kelly_stake", 0.0),
                    row.get("pinnacle_american_this_side"),
                    row.get("pinnacle_american_other_side"),
                    row["pinnacle_overround"],
                    _utc_iso(row.get("pinnacle_quote_time")),
                    _utc_iso(row.get("book_quote_time")),
                    row.get("flags", ""),
                    row.get("pricing_method", "exact"),
                    row.get("line_adjustment_method", ""),
                    row.get("pinnacle_line"),
                    row.get("pinnacle_lines_used", ""),
                    row.get("win_probability"),
                    row.get("lose_probability"),
                    row.get("push_probability"),
                )
                for row in opportunities
            ],
        )


def finalize_started_opportunities(
    now: datetime | None = None, limit: int | None = None
) -> int:
    """Finalize settled CLV rows, optionally capped to bound memory per call."""
    init_db()
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc).isoformat()
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.row_factory = sqlite3.Row
        sql = """
            SELECT id, sport, event_id, market_key, period, team, selection,
                   commence_time, line, odds_decimal, pricing_method
            FROM ev_opportunity_log
            WHERE closing_fair_probability IS NULL
              AND commence_time <= ?
            ORDER BY commence_time
        """
        params: list = [now_utc]
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        opportunities = conn.execute(sql, params).fetchall()
        finalized = 0
        for opportunity in opportunities:
            snapshots = conn.execute(
                """
                SELECT scan_id, observed_at, line, fair_probability, overround,
                       quote_time
                FROM pinnacle_market_snapshots
                WHERE sport = ? AND event_id = ? AND market_key = ?
                  AND period = ? AND team = ? AND selection = ?
                  AND quote_time IS NOT NULL
                  AND quote_time >= ? AND quote_time <= ?
                """,
                (
                    opportunity["sport"],
                    opportunity["event_id"],
                    opportunity["market_key"],
                    opportunity["period"],
                    opportunity["team"],
                    opportunity["selection"],
                    *_closing_window(opportunity["commence_time"]),
                ),
            ).fetchall()
            closing_probability = None
            closing_quote_time = None
            closing_win = None
            closing_lose = None
            closing_push = None
            clv_pct = None

            if opportunity["pricing_method"] == "line-adjusted":
                snapshots_by_scan: dict[str, list[sqlite3.Row]] = defaultdict(list)
                for snapshot in snapshots:
                    snapshots_by_scan[snapshot["scan_id"]].append(snapshot)
                scan_groups = sorted(
                    snapshots_by_scan.values(),
                    key=lambda group: max(
                        (snapshot["quote_time"], snapshot["observed_at"])
                        for snapshot in group
                    ),
                    reverse=True,
                )
                for group in scan_groups:
                    references = [
                        {
                            "point": snapshot["line"],
                            "fair_probability": snapshot["fair_probability"],
                            "overround": snapshot["overround"],
                            "quote_time": snapshot["quote_time"],
                        }
                        for snapshot in group
                        if snapshot["line"] is not None
                    ]
                    estimate = estimate_line_adjusted_probability(
                        opportunity["sport"],
                        opportunity["market_key"],
                        opportunity["selection"],
                        float(opportunity["line"]),
                        references,
                        CONFIG,
                    )
                    if estimate is None:
                        continue
                    closing_probability = estimate["fair_probability"]
                    closing_win = estimate["win_probability"]
                    closing_lose = estimate["lose_probability"]
                    closing_push = estimate["push_probability"]
                    used_times = [
                        reference.get("quote_time")
                        for reference in estimate["source_references"]
                        if reference.get("quote_time")
                    ]
                    closing_quote_time = max(used_times) if used_times else None
                    clv_pct = expected_value_with_push_pct(
                        opportunity["odds_decimal"],
                        closing_win,
                        closing_lose,
                    )
                    break
            else:
                matching = [
                    snapshot
                    for snapshot in snapshots
                    if (
                        (snapshot["line"] is None and opportunity["line"] is None)
                        or (
                            snapshot["line"] is not None
                            and opportunity["line"] is not None
                            and round(snapshot["line"], 6)
                            == round(opportunity["line"], 6)
                        )
                    )
                ]
                if matching:
                    closing_snapshot = max(
                        matching,
                        key=lambda snapshot: (
                            snapshot["quote_time"],
                            snapshot["observed_at"],
                        ),
                    )
                    closing_probability = closing_snapshot["fair_probability"]
                    closing_quote_time = closing_snapshot["quote_time"]
                    closing_win = closing_probability
                    closing_lose = 1 - closing_probability
                    closing_push = 0.0
                    if (
                        opportunity["line"] is not None
                        and opportunity["market_key"].split(":", 1)[0]
                        in {"spreads", "totals"}
                    ):
                        exact_estimate = estimate_line_adjusted_probability(
                            opportunity["sport"],
                            opportunity["market_key"].split(":", 1)[0],
                            opportunity["selection"],
                            float(opportunity["line"]),
                            [
                                {
                                    "point": closing_snapshot["line"],
                                    "fair_probability": closing_probability,
                                    "overround": closing_snapshot["overround"],
                                    "quote_time": closing_quote_time,
                                }
                            ],
                            CONFIG,
                        )
                        if exact_estimate is not None:
                            closing_win = exact_estimate["win_probability"]
                            closing_lose = exact_estimate["lose_probability"]
                            closing_push = exact_estimate["push_probability"]
                            clv_pct = expected_value_with_push_pct(
                                opportunity["odds_decimal"],
                                closing_win,
                                closing_lose,
                            )
                    if clv_pct is None:
                        clv_pct = (
                            opportunity["odds_decimal"] * closing_probability - 1
                        ) * 100

            if closing_probability is None or closing_quote_time is None:
                continue
            conn.execute(
                """
                UPDATE ev_opportunity_log
                SET closing_fair_probability = ?, closing_quote_time = ?,
                    clv_pct = ?, closing_win_probability = ?,
                    closing_lose_probability = ?, closing_push_probability = ?,
                    finalized_at = ?
                WHERE id = ?
                """,
                (
                    closing_probability,
                    closing_quote_time,
                    clv_pct,
                    closing_win,
                    closing_lose,
                    closing_push,
                    now_utc,
                    opportunity["id"],
                ),
            )
            finalized += 1
        return finalized


EDGE_KEY_COLUMNS = [
    "sport",
    "event_id",
    "market",
    "period",
    "selection",
    "sportsbook",
    "line",
]


def summarize_clv_edges(opportunity_log: pd.DataFrame) -> dict:
    """Count each edge once (first observation) and summarize its closed rows."""
    empty = {"logged": 0, "closed": 0, "mean_clv_pct": None, "mean_ev_pct": None}
    if opportunity_log.empty:
        return empty
    deduplicated = opportunity_log.sort_values("observed_at").drop_duplicates(
        subset=EDGE_KEY_COLUMNS, keep="first"
    )
    closed = deduplicated[deduplicated["clv_pct"].notna()]
    return {
        "logged": len(deduplicated),
        "closed": len(closed),
        "mean_clv_pct": float(closed["clv_pct"].mean()) if not closed.empty else None,
        "mean_ev_pct": float(closed["ev_pct"].mean()) if not closed.empty else None,
    }


def load_opportunity_log() -> pd.DataFrame:
    init_db()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return pd.read_sql_query(
            """
            SELECT observed_at, sport, event_id, commence_time, matchup, market,
                   period, selection,
                   sportsbook, odds_american, odds_decimal, line,
                   pinnacle_line, pinnacle_lines_used, pricing_method,
                   line_adjustment_method, fair_probability, win_probability,
                   lose_probability, push_probability,
                   ev_pct, kelly_stake, pinnacle_american_this_side,
                   pinnacle_american_other_side, pinnacle_overround,
                   pinnacle_quote_time, book_quote_time, flags,
                   closing_fair_probability, closing_quote_time, clv_pct,
                   closing_win_probability, closing_lose_probability,
                   closing_push_probability,
                   finalized_at
            FROM ev_opportunity_log
            ORDER BY observed_at DESC, ev_pct DESC
            """,
            conn,
        )
