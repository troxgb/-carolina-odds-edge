#!/usr/bin/env python3
"""
Standalone local/VPS cron worker for automated +EV and Arbitrage alerts.

The worker is intentionally decoupled from Git: it reads configuration from the
environment (or a local ``.env`` file), writes JSON state atomically, records
scans in ``data/bets.db`` through SQLite transactions, and exits after one
scan-and-alert cycle so a cron entry can run it on a schedule.

Player props are arbitrage-only. They are never evaluated for +EV, alerted as
+EV, or logged as Pinnacle +EV/CLV snapshots.
"""

from __future__ import annotations

import html
import json
import math
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any
from uuid import uuid4

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.util import connection as urllib3_connection

# A local .env file keeps the VPS cron install self-contained. Environment
# variables always win because python-dotenv does not override existing values.
try:  # pragma: no cover - exercised implicitly when python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from ev_engine import (
    calculate_capped_kelly_stakes,
    calculate_required_kelly_stakes,
    decimal_to_american,
)
from propline_api import (
    SPORT_CONFIG,
    fetch_freshness,
    fetch_injuries,
    fetch_sport_events,
    injured_player_names,
    record_quota,
    remaining_quota_for_key,
    run_api,
    select_api_keys,
    stale_book_keys,
)
from scanner_config import CONFIG
from scanner_logic import (
    _normalize_player_name,
    _parse_utc,
    find_arbitrage_opportunities,
    is_player_prop_quote,
    scan_pinnacle_ev,
    size_arbitrage_stakes,
)

# Master list of sports the worker is allowed to consider. Auto-selection below
# further restricts this to high-volume Tier 1 team sports, and only when they
# have an upcoming game within the next 7 days. Tennis was dropped because
# PropLine's start times for it are unreliable by hours. Tier 3 sports (golf,
# racing, esports, etc.) are never auto-included to avoid thin markets and
# elevated limit/ban risk; add them here manually if desired.
ACTIVE_SPORTS = [
    "NCAAB",
    "NBA",
    "WNBA",
    "NHL",
    "NFL",
    "NCAAF",  # College Football (CFB)
    "UFC",
    # "MLB",  # Offseason
    # "MLS",  # Optional: lower volume, add manually if desired
]

# Sports that are safe to auto-scan: high betting volume + exchange coverage.
# Everything else requires an explicit entry in ACTIVE_SPORTS above AND a manual
# opt-in (kept out of this set so thin/obscure markets are never auto-scanned).
AUTO_INCLUDE_SPORTS = {
    "NFL",
    "NCAAF",
    "NBA",
    "WNBA",
    "NCAAB",
    "NHL",
    "UFC",
}

# How far ahead a sport needs an event to be considered "in season".
SPORT_LOOKAHEAD_DAYS = 7

# Per-sport lead time for *scanning*, capped by SPORT_LOOKAHEAD_DAYS. Early
# summer lines on the big team sports are thin and rarely move until closer to
# the game; UFC is the opposite, with the heaviest movement during fight week.
# These are starting points that can be tuned without touching the scan loop.
SPORT_SCAN_LEAD_HOURS = {
    "NBA": 48,
    "NCAAB": 48,
    "NHL": 48,
    "WNBA": 48,
    "NFL": 96,
    "NCAAF": 96,
    "UFC": 24 * 7,
}


def scan_lead_hours(sport: str) -> int:
    """Lead time in hours for a sport, never exceeding the global 7-day cap."""
    lead = SPORT_SCAN_LEAD_HOURS.get(str(sport).upper(), SPORT_LOOKAHEAD_DAYS * 24)
    return min(lead, SPORT_LOOKAHEAD_DAYS * 24)


def sport_has_event_in_window(
    sport: str,
    events: list[dict[str, Any]] | None,
    now_utc: datetime,
) -> bool:
    """True when the sport has a live event or one inside its own lead window."""
    cutoff = now_utc + timedelta(hours=scan_lead_hours(sport))
    for event in events or []:
        if event.get("completed"):
            continue
        if event.get("live"):
            return True
        commence = _parse_utc(event.get("commence_time"))
        if commence is not None and now_utc <= commence <= cutoff:
            return True
    return False

ALERT_HISTORY_FILE = "alert_history.json"
CACHED_ODDS_FILE = "cached_odds.json"
DEFAULT_MIN_EV_THRESHOLD = CONFIG.worker_min_ev_pct  # 3.5% automated floor
DEFAULT_MAX_EV_THRESHOLD = CONFIG.worker_max_ev_pct  # None: no upper cap
DEFAULT_MIN_EV_INCREASE_FOR_RE_ALERT = 1.5
DEFAULT_MIN_ROI_INCREASE_FOR_ARB = 0.5
MAX_HISTORY_AGE_SECONDS = 48 * 3600  # 48 hours

# Risk and Bankroll Limits: Never suggest a single bet over $300.
MAX_SINGLE_BET = 300.0
DEFAULT_MIN_ARB_ROI = 1.0  # 1.0% minimum ROI for Arbitrage alerts
ARBITRAGE_TARGET_TOTAL_STAKE = 300.0  # Total stake per arb card (bankroll-sized)

# --- Alert filters (tuned for a $1,000 bankroll and a low-notification goal) ---
ET_ZONE = ZoneInfo("America/New_York")
EV_AMERICAN_MIN = -150  # Only odds from -150 up through +200 for +EV alerts
EV_AMERICAN_MAX = 200
ARB_ROI_MIN_PCT = 1.0
ARB_MIN_PROFIT_DOLLARS = 10.0  # Minimum guaranteed profit per alerted arb card
ARB_MAX_TOTAL_STAKE_DOLLARS = 300.0  # Maximum total stake (both legs) per arb card
PEAK_START_HOUR_ET = 15  # 3:00 PM Eastern (weekdays)
PEAK_END_HOUR_ET = 20  # 8:00 PM Eastern (exclusive, weekdays)
WEEKEND_PEAK_START_HOUR_ET = 9  # 9:00 AM Eastern (weekends)
WEEKEND_PEAK_END_HOUR_ET = 23  # 11:00 PM Eastern (weekends, exclusive)

# Per-scan alert caps: send at most this many alerts per run, picking the best.
MAX_EV_ALERTS_PER_SCAN = 3
MAX_ARB_ALERTS_PER_SCAN = 5
# Daily alert caps removed: alert every qualifying opportunity (subject to
# dedup history and peak window for +EV).
DAILY_EV_ALERT_LIMIT = 10**9
DAILY_ARB_ALERT_LIMIT = 10**9

CANT_MISS_SPORTS = {"nfl", "nba"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


ALERT_SEND_DELAY_SECONDS = _env_float("WORKER_ALERT_DELAY_SECONDS", 0.25)


def _env_optional_float(name: str, default: float | None) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    if raw.casefold() in {"none", "off", "disabled", "uncapped"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def worker_ev_settings() -> tuple[float, float | None]:
    """Read the automated +EV floor/cap from the environment."""
    min_ev = _env_float("WORKER_MIN_EV_PCT", CONFIG.worker_min_ev_pct)
    max_ev = _env_optional_float("WORKER_MAX_EV_PCT", CONFIG.worker_max_ev_pct)
    if max_ev is not None and max_ev < min_ev:
        max_ev = None
    return min_ev, max_ev


# --- IPv4 Adapter for Telegram API (prevents IPv6 connection resets) ---
class _IPv4HTTPSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:
        address = socket.getaddrinfo(
            self._dns_host, self.port, socket.AF_INET, socket.SOCK_STREAM
        )[0][4][0]
        return urllib3_connection.create_connection(
            (address, self.port),
            self.timeout,
            source_address=self.source_address,
            socket_options=self.socket_options,
        )


class _IPv4HTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _IPv4HTTPSConnection


class _TelegramIPv4Adapter(HTTPAdapter):
    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any) -> None:
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(self.poolmanager.pool_classes_by_scheme)
        self.poolmanager.pool_classes_by_scheme["https"] = _IPv4HTTPSConnectionPool


def _new_telegram_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://api.telegram.org/", _TelegramIPv4Adapter())
    return session


# ---------------------------------------------------------------------------
# Atomic state files
# ---------------------------------------------------------------------------


def _replace_with_retry(temporary_path: str, path: str) -> None:
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            os.replace(temporary_path, path)
            return
        except PermissionError as error:  # Windows can briefly lock the file.
            last_error = error
            time.sleep(0.05 * (attempt + 1))
    if os.path.exists(temporary_path):
        os.remove(temporary_path)
    if last_error is not None:
        raise last_error


def _atomic_write_text(path: str, text: str) -> None:
    """Write ``path`` atomically through a sibling ``.tmp`` file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
    _replace_with_retry(temporary_path, path)


def _atomic_write_json(path: str, data: Any) -> None:
    """Write ``path`` atomically without materialising the whole JSON string.

    ``json.dumps`` over a cached scan (~130k quotes) built a >78 MB string on
    top of the in-memory quotes, which is enough to exhaust a 1 GB VPS. Compact
    separators also cut the on-disk cache by roughly a third.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, separators=(",", ":"))
        handle.flush()
    _replace_with_retry(temporary_path, path)


def _load_cached_json(path: str, ttl_seconds: float) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    cached_at = payload.get("_cached_at")
    if not isinstance(cached_at, (int, float)):
        return None
    if time.time() - float(cached_at) > ttl_seconds:
        return None
    return payload


def _save_cached_json(path: str, data: dict[str, Any]) -> None:
    payload = dict(data)
    payload["_cached_at"] = time.time()
    _atomic_write_json(path, payload)


# ---------------------------------------------------------------------------
# Environment credentials
# ---------------------------------------------------------------------------


def get_api_keys(now: datetime | None = None) -> list[str]:
    """Return ordered PropLine keys (UTC half-day primary first, then backups)."""
    keys: list[str] = []

    def add(key: str) -> None:
        key = key.strip()
        if key and key not in keys:
            keys.append(key)

    # Honor the persisted quota state so a key that is already below the
    # failover threshold does not become the primary for this scan.
    for key in select_api_keys(
        now, os.getenv("QUOTA_STATE_FILE", CONFIG.quota_state_file)
    ):
        add(key)

    for key_var in ("PROPLINE_API_KEYS", "PROP_LINE_API_KEYS"):
        val = os.getenv(key_var, "").strip()
        if val:
            for part in val.split(","):
                add(part)

    for key_var in ("PROPLINE_API_KEY", "PROP_LINE_API_KEY", "API_KEY"):
        add(os.getenv(key_var, ""))

    for key_var in (
        "PROPLINE_API_KEY_BACKUP",
        "PROP_LINE_API_KEY_BACKUP",
        "PROPLINE_BACKUP_KEY",
        "API_KEY_BACKUP",
    ):
        add(os.getenv(key_var, ""))

    return keys


def get_api_key(now: datetime | None = None) -> str:
    keys = get_api_keys(now)
    return keys[0] if keys else ""


def get_telegram_credentials() -> tuple[str, str]:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return bot_token, chat_id


def get_discord_webhook() -> str:
    return os.getenv("DISCORD_WEBHOOK_URL", "").strip()


def _quota_state_path() -> str:
    return os.getenv("QUOTA_STATE_FILE", CONFIG.quota_state_file)


def _get_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _get_float_env(name: str, default: float) -> float:
    return _env_float(name, default)


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------


def is_game_started(
    commence_time_str: str | None,
    now_utc: datetime,
    guard_minutes: float = 0.0,
) -> bool:
    """True when the event is live or within ``guard_minutes`` of kickoff.

    The guard exists because provider clocks are not always right: PropLine's
    team-sport start times can be 30 minutes late, so treating the advertised
    kickoff as final let alerts fire for games that were already under way.
    """
    if not commence_time_str or not isinstance(commence_time_str, str):
        return False
    try:
        dt = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= now_utc + timedelta(minutes=guard_minutes)
    except (ValueError, TypeError):
        return False


def select_scan_sports(
    configured: list[str] | None = None,
    cached: dict[str, Any] | None = None,
) -> list[str]:
    """
    Drop explicitly configured off-season sports and any sport whose latest
    cached scan saw zero events. This avoids spending one credit on empty
    slates while keeping sports that have never been scanned.
    """
    configured = configured or ACTIVE_SPORTS
    offseason_raw = os.getenv("WORKER_OFFSEASON_SPORTS", "")
    offseason = {
        item.strip().casefold()
        for item in offseason_raw.split(",")
        if item.strip()
    }
    candidates = [
        sport for sport in configured if sport.casefold() not in offseason
    ]
    if not cached:
        return candidates

    cached_sports = {
        str(sport) for sport in cached.get("sports", []) if sport
    }
    quote_counts: dict[str, int] = {}
    for quote in cached.get("quotes", []):
        sport = str(quote.get("sport") or "")
        if sport:
            quote_counts[sport] = quote_counts.get(sport, 0) + 1

    selected: list[str] = []
    for sport in candidates:
        if sport in cached_sports and quote_counts.get(sport, 0) == 0:
            print(
                f"Skipping {sport}: the latest cached scan found no upcoming "
                "or live events (off-season or empty slate)."
            )
            continue
        selected.append(sport)
    return selected


# ---------------------------------------------------------------------------
# Alert keys & deduplication
# ---------------------------------------------------------------------------


def build_alert_key(opp: dict[str, Any]) -> str:
    """{game_id}_{market}_{selection}_{line}_{sportsbook}"""
    game_id = str(opp.get("event_id") or opp.get("game_id") or "")
    market = str(opp.get("market") or opp.get("market_key") or "")
    selection = str(opp.get("selection") or "")
    line_val = opp.get("line") if opp.get("line") is not None else opp.get("point")
    line_str = str(line_val) if line_val is not None else "None"
    sportsbook = str(opp.get("sportsbook") or "")
    return f"{game_id}_{market}_{selection}_{line_str}_{sportsbook}"


def build_arbitrage_alert_key(arb: dict[str, Any]) -> str:
    """arb_{game_id}_{market}[_{player}][_{line}]_{sorted_legs}"""
    game_id = str(arb.get("event_id") or "")
    market = str(arb.get("market") or arb.get("market_key") or "")
    parts = ["arb", game_id, market]
    player = str(arb.get("player_id") or arb.get("player_name") or "")
    if player:
        parts.append(player)
    line = arb.get("line")
    if line is not None:
        parts.append(str(line))
    legs_repr = "-".join(
        sorted(
            f"{leg.get('selection')}_{leg.get('sportsbook')}"
            for leg in arb.get("legs", [])
        )
    )
    parts.append(legs_repr)
    return "_".join(parts)


def load_alert_history(path: str = ALERT_HISTORY_FILE) -> dict[str, dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception as error:
        print(f"Warning: Could not read {path}: {error}. Starting with empty history.")
        return {}


def save_alert_history(
    history: dict[str, dict[str, Any]], path: str = ALERT_HISTORY_FILE
) -> None:
    try:
        _atomic_write_json(path, history)
    except Exception as error:
        print(f"Error: Failed to save {path}: {error}", file=sys.stderr)


def clean_expired_history(
    history: dict[str, dict[str, Any]],
    now_ts: float,
    max_age_seconds: float = MAX_HISTORY_AGE_SECONDS,
) -> dict[str, dict[str, Any]]:
    cleaned = {}
    for key, entry in history.items():
        if not isinstance(entry, dict):
            continue
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, (int, float)) and (now_ts - timestamp) <= max_age_seconds:
            cleaned[key] = entry
    return cleaned


def should_alert(
    key: str,
    current_ev: float,
    history: dict[str, dict[str, Any]],
    min_increase: float = DEFAULT_MIN_EV_INCREASE_FOR_RE_ALERT,
) -> bool:
    if key not in history:
        return True
    prev_entry = history[key]
    prev_ev = float(prev_entry.get("ev", 0.0))
    return (current_ev - prev_ev) >= min_increase


def should_alert_arbitrage(
    key: str,
    current_roi: float,
    history: dict[str, dict[str, Any]],
    min_increase: float = DEFAULT_MIN_ROI_INCREASE_FOR_ARB,
) -> bool:
    if key not in history:
        return True
    prev_entry = history[key]
    prev_roi = float(prev_entry.get("roi", 0.0))
    return (current_roi - prev_roi) >= min_increase


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_kelly_units(
    stake: float, bankroll: float = CONFIG.bankroll, max_single_bet: float = MAX_SINGLE_BET
) -> str:
    """
    Format a Kelly stake in 0.25u increments (1u = 1% of bankroll), capped at
    ``max_single_bet``.
    """
    effective_stake = min(stake, max_single_bet)
    unit_size = bankroll * 0.01
    if effective_stake <= 0 or unit_size <= 0:
        return "0u"
    raw_units = effective_stake / unit_size
    rounded = round(raw_units * 4) / 4
    if rounded == 0 and effective_stake > 0:
        rounded = 0.25
    if rounded.is_integer():
        return f"{int(rounded)}u"
    return f"{rounded:g}u"


def _format_american(odds_american: Any, odds_decimal: Any) -> str:
    if odds_american is not None:
        try:
            odds_str = f"{int(odds_american):+d}"
        except (ValueError, TypeError):
            odds_str = str(odds_american)
    elif odds_decimal is not None:
        try:
            odds_str = f"{decimal_to_american(float(odds_decimal)):+d}"
        except Exception:
            odds_str = "N/A"
    else:
        odds_str = "N/A"
    if odds_decimal is not None:
        try:
            odds_str += f" ({float(odds_decimal):.2f})"
        except (ValueError, TypeError):
            pass
    return odds_str


def _format_point(value: Any, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:+g}" if signed else f"{number:g}"


def _escaped(value: Any) -> str:
    """Escape user/feed text for Telegram HTML parse mode."""
    return html.escape(str(value if value is not None else ""), quote=True)


def format_telegram_alert(opp: dict[str, Any]) -> str:
    """Format a +EV opportunity for Telegram HTML."""
    sport = _escaped(opp.get("sport") or "Unknown")
    matchup = _escaped(opp.get("matchup") or "Unknown Matchup")
    selection = _escaped(opp.get("selection") or "")
    market_raw = str(opp.get("market") or opp.get("market_key") or "")
    line = opp.get("line") if opp.get("line") is not None else opp.get("point")
    player_name = str(opp.get("player_name") or "").strip()

    if "team" in market_raw.lower():
        market_label = f"{opp.get('team', '')} Team Total".strip()
    elif "spread" in market_raw.lower():
        market_label = "Spread"
    elif "total" in market_raw.lower():
        market_label = "Total"
    elif "h2h" in market_raw.lower() or "moneyline" in market_raw.lower():
        market_label = "Moneyline"
    else:
        market_label = market_raw

    market_label = _escaped(market_label)
    if line is not None:
        line_text = _format_point(line, signed="spread" in market_raw.lower())
        player_prefix = f"{_escaped(player_name)} " if player_name else ""
        bet_line = f"{player_prefix}{selection} {line_text} ({market_label})"
    else:
        player_prefix = f"{_escaped(player_name)} " if player_name else ""
        bet_line = (
            f"{player_prefix}{selection} ({market_label})"
            if market_label
            else f"{player_prefix}{selection}"
        )

    odds_str = _format_american(opp.get("odds_american"), opp.get("odds_decimal"))
    sportsbook = _escaped(opp.get("sportsbook") or "Unknown")

    fair_str = "N/A"
    fair_prob = opp.get("fair_probability")
    if fair_prob is not None:
        try:
            fp = float(fair_prob)
            if 0 < fp < 1:
                fair_dec = 1.0 / fp
                fair_am = decimal_to_american(fair_dec)
                fair_str = f"{fair_am:+d} ({fair_dec:.2f}) [Fair Prob: {fp * 100:.1f}%]"
        except Exception:
            pass

    ev_pct = float(opp.get("ev_pct", 0.0))
    exec_line = ""
    exec_dec = opp.get("odds_decimal_executable")
    quoted_dec = opp.get("odds_decimal")
    try:
        if exec_dec and quoted_dec and float(exec_dec) < float(quoted_dec) - 1e-9:
            exec_am = decimal_to_american(float(exec_dec))
            exec_ev = float(opp.get("executable_ev_pct", 0.0))
            detail = (
                f"{exec_am:+d} at ${CONFIG.executable_odds_probe_stake:.0f} "
                f"(EV +{exec_ev:.2f}%)"
            )
            liquidity = opp.get("liquidity")
            if isinstance(liquidity, (int, float)) and not isinstance(liquidity, bool):
                detail += f", top ${float(liquidity):,.0f}"
            exec_line = f"\n📉 <b>Executable Odds:</b> {_escaped(detail)}"
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    raw_stake = opp.get("kelly_stake")
    if raw_stake is None and opp.get("fair_probability") and opp.get("odds_decimal"):
        try:
            from ev_engine import calculate_kelly

            quote_fraction = opp.get("kelly_fraction", CONFIG.kelly_fraction)
            net_decimal = float(opp.get("odds_decimal_net", opp["odds_decimal"]))
            kelly_value = calculate_kelly(
                CONFIG.bankroll,
                float(opp["fair_probability"]),
                net_decimal,
                quote_fraction,
            )
            raw_stake = min(
                float(kelly_value), CONFIG.bankroll * CONFIG.max_bet_pct
            )
        except Exception:
            raw_stake = 0.0

    stake = min(float(raw_stake or 0.0), MAX_SINGLE_BET)
    units_str = format_kelly_units(stake)
    if opp.get("flags") and "verify" in str(opp.get("flags")):
        stake_text = "0u <i>(Flagged for verification)</i>"
    elif stake > 0:
        stake_text = f"{units_str} <i>(1u = 1% of bankroll)</i>"
    else:
        stake_text = "0u <i>(1u = 1% of bankroll)</i>"

    min_bet_line = ""
    required = _finite_positive(
        opp.get("min_bet_amount")
        if opp.get("min_bet_amount") is not None
        else opp.get("required_stake")
    )
    if required is not None:
        min_bet_line = (
            f"\n🔻 <b>Min Bet:</b> ${required:,.2f} "
            "<i>(Kelly requirement, net of exchange fees)</i>"
        )

    return (
        f"🚨 <b>+EV Bet Opportunity</b> 🚨\n\n"
        f"🏆 <b>Sport:</b> {sport}\n"
        f"⚔️ <b>Matchup:</b> {matchup}\n"
        f"🎯 <b>Bet / Line:</b> {bet_line}\n"
        f"💰 <b>Odds:</b> {_escaped(odds_str)}{exec_line}\n"
        f"🏢 <b>Sportsbook:</b> {sportsbook}\n"
        f"⚖️ <b>Fair Odds:</b> {_escaped(fair_str)}\n"
        f"📈 <b>EV%:</b> +{ev_pct:.2f}%{min_bet_line}\n"
        f"💵 <b>Recommended Stake:</b> {stake_text}"
    )


def _arb_leg_display(leg: dict[str, Any], arb: dict[str, Any]) -> str:
    selection = _escaped(leg.get("selection") or "")
    player_name = str(
        leg.get("player_name") or arb.get("player_name") or ""
    ).strip()
    line = leg.get("line") if leg.get("line") is not None else arb.get("line")
    if player_name:
        line_text = f" {_format_point(line)}" if line is not None else ""
        return f"{_escaped(player_name)} {selection}{line_text}"
    if line is not None:
        market_text = str(arb.get("market") or "").casefold()
        signed = "spread" in market_text
        return f"{selection} {_format_point(line, signed=signed)}"
    return selection


def format_telegram_arbitrage_alert(arb: dict[str, Any]) -> str:
    """Format an arbitrage opportunity for Telegram HTML."""
    sport = _escaped(arb.get("sport") or "Unknown")
    matchup = _escaped(arb.get("matchup") or "Unknown Matchup")
    market = _escaped(arb.get("market") or "")
    player_name = str(arb.get("player_name") or "").strip()
    if player_name and market:
        market = f"{_escaped(player_name)} · {market}"
    total_stake = float(arb.get("total_stake", 0.0))
    profit = float(arb.get("profit", 0.0))
    roi_pct = float(arb.get("roi_pct", 0.0))
    locked_payout = float(arb.get("locked_payout", total_stake + profit))
    minimum_profit = arb.get("min_guaranteed_profit")
    if minimum_profit is None:
        minimum_profit = CONFIG.min_arb_guaranteed_profit_dollars

    legs_html = []
    for index, leg in enumerate(arb.get("legs", []), start=1):
        book = _escaped(leg.get("sportsbook") or "")
        bet_display = _arb_leg_display(leg, arb)
        stake = float(leg.get("stake", 0.0))
        payout = float(leg.get("payout", 0.0))
        odds_str = _format_american(
            leg.get("odds_american"), leg.get("odds_decimal")
        )
        net_note = ""
        quoted_decimal = leg.get("odds_decimal_quoted")
        net_decimal = leg.get("odds_decimal")
        if quoted_decimal is not None and net_decimal is not None:
            try:
                if float(quoted_decimal) - float(net_decimal) > 1e-9:
                    net_note = " <i>(net of exchange fee)</i>"
            except (TypeError, ValueError):
                pass
        liquidity = leg.get("liquidity")
        liquidity_text = ""
        if isinstance(liquidity, (int, float)) and not isinstance(liquidity, bool):
            liquidity_text = f"\n• Liquidity: ${float(liquidity):,.2f}"

        legs_html.append(
            f"<b>Leg {index}:</b>\n"
            f"• Bet: <b>{bet_display}</b> at <b>{book}</b>\n"
            f"• Odds: {_escaped(odds_str)}{net_note}\n"
            f"• Stake: <b>${stake:,.0f}</b> (Payout: ${payout:,.2f})"
            f"{liquidity_text}"
        )

    legs_text = "\n\n".join(legs_html)

    return (
        f"⚡ <b>Arbitrage Opportunity (Guaranteed Profit)</b> ⚡\n\n"
        f"🏆 <b>Sport:</b> {sport}\n"
        f"⚔️ <b>Matchup:</b> {matchup}\n"
        f"🎯 <b>Market:</b> {market}\n"
        f"💰 <b>Total Stake:</b> ${total_stake:,.0f} (max $300 per bet)\n"
        f"💵 <b>Guaranteed Profit:</b> +${profit:.2f} (ROI: +{roi_pct:.2f}%)\n"
        f"🔒 <b>Locked Payout:</b> ${locked_payout:,.2f}\n"
        f"✅ <b>Meets minimum:</b> ${float(minimum_profit):,.2f}\n\n"
        f"{legs_text}"
    )


def format_welcome_guide(greeting_name: str | None = None) -> str:
    greeting = (
        f"👋 <b>Welcome to Not Your Bookie Edge, {_escaped(greeting_name)}!</b> 🎯"
        if greeting_name
        else "👋 <b>Welcome to Not Your Bookie Edge!</b> 🎯"
    )
    return (
        f"{greeting}\n\n"
        f"Here is your quick-start guide on how our automated mathematical alerts work and how to maximize your profits:\n\n"
        f"📚 <b>Need-to-Know Terms:</b>\n\n"
        f"• <b>What is +EV (Expected Value)?</b>\n"
        f"We scan thousands of real-time odds against Pinnacle (the sharpest bookmaker in the world). When a sportsbook misprices a line, we get a mathematical edge over the house. Over time, betting +EV is mathematically proven to generate profit.\n\n"
        f"• <b>What is a 'Unit' (u)?</b>\n"
        f"A unit is a standardized bet size based on <b>your own bankroll</b>:\n"
        f"👉 <b>1 unit (1u) = 1% of your total betting bankroll.</b>\n"
        f"<i>Examples:</i>\n"
        f"• $500 bankroll ➔ 1u = $5 (0.5u = $2.50, 0.75u = $3.75)\n"
        f"• $1,000 bankroll ➔ 1u = $10 (0.75u = $7.50, 1.25u = $12.50)\n"
        f"• $5,000 bankroll ➔ 1u = $50 (0.5u = $25.00, 1.5u = $75.00)\n"
        f"Never risk more than the recommended unit size to protect against normal variance.\n\n"
        f"• <b>What is an Arbitrage Alert (⚡)?</b>\n"
        f"A 100% risk-free trade where two sportsbooks disagree on odds. You bet both sides and lock in a guaranteed profit before the game even starts. Minimum ${ARB_MIN_PROFIT_DOLLARS:,.2f} guaranteed profit, any ROI above 1%.\n\n"
        f"• <b>Fair Odds:</b>\n"
        f"The true 'no-vig' probability. As long as your book offers odds equal to or better than Fair Odds, the bet has positive value.\n\n"
        f"📱 <b>Books We Scan:</b>\n"
        f"DraftKings, FanDuel, BetMGM, Fanatics, theScore Bet, Novig, ProphetX, and Pinnacle (reference only).\n"
        f"Arbitrage requires two <b>different</b> books on each leg — Pinnacle is never used as a betting leg.\n\n"
        f"🏟️ <b>Sports We Scan:</b>\n"
        f"NFL, NCAAF, NBA, WNBA, NCAAB, NHL, UFC (when in season). "
        f"Only sports with upcoming games are scanned to save resources.\n\n"
        f"⚠️ <b>Responsible Gambling:</b>\n"
        f"If betting stops being fun, call 1-800-GAMBLER (24/7, free, confidential).\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 <b>How to Read Alerts:</b>\n"
        f"1. <b>Bet / Line:</b> The exact wager to select (Spread, Total, Moneyline).\n"
        f"2. <b>Sportsbook:</b> The app offering the mispriced line (DraftKings, FanDuel, etc.).\n"
        f"3. <b>Odds:</b> The price to take. If the odds drop below the alert price, skip it.\n"
        f"4. <b>Recommended Stake:</b> The exact unit size to place (e.g., 0.5u, 1u).\n\n"
        f"⚡ <b>Pro-Tip:</b> Lines move fast! When an alert drops, place your wager promptly before the sportsbook adjusts the price."
    )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def send_telegram_message(
    bot_token: str, chat_id: str, message: str, timeout: int = 15
) -> bool:
    """Send a message via Telegram Bot API sendMessage using an IPv4 session."""
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    session = _new_telegram_session()
    try:
        for attempt in range(3):
            response = session.post(url, json=payload, timeout=timeout)
            if response.status_code == 200:
                return True
            if response.status_code == 429 and attempt < 2:
                retry_after = 3
                try:
                    retry_after = int(
                        (response.json().get("parameters") or {}).get(
                            "retry_after", retry_after
                        )
                    )
                except (ValueError, TypeError, AttributeError):
                    pass
                retry_after = max(1, min(retry_after, 30))
                print(
                    f"Telegram rate limited (HTTP 429); retrying in {retry_after}s.",
                    file=sys.stderr,
                )
                time.sleep(retry_after)
                continue
            print(
                f"Telegram API error (HTTP {response.status_code}): {response.text}",
                file=sys.stderr,
            )
            return False
        return False
    except requests.RequestException as error:
        print(f"Telegram request failed: {error}", file=sys.stderr)
        return False
    finally:
        session.close()


def _plain_text(message: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", "", message)
    return html.unescape(text)


def send_discord_message(
    webhook_url: str, message: str, timeout: int = 15
) -> bool:
    """Optional Discord fallback for installs that still configure a webhook."""
    if not webhook_url:
        return False
    try:
        response = requests.post(
            webhook_url,
            json={"content": _plain_text(message)[:1900]},
            timeout=timeout,
        )
        if response.status_code in {200, 204}:
            return True
        print(
            f"Discord webhook error (HTTP {response.status_code}): {response.text}",
            file=sys.stderr,
        )
    except requests.RequestException as error:
        print(f"Discord request failed: {error}", file=sys.stderr)
    return False


def dispatch_alert(message: str) -> bool:
    """Send Telegram first, then fall back to Discord if configured."""
    bot_token, chat_id = get_telegram_credentials()
    if bot_token and chat_id and send_telegram_message(bot_token, chat_id, message):
        return True
    webhook = get_discord_webhook()
    if webhook and send_discord_message(webhook, message):
        return True
    return False


def check_and_welcome_new_members(bot_token: str, chat_id: str) -> int:
    if not bot_token or not chat_id:
        return 0
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    session = _new_telegram_session()
    welcomed = 0
    try:
        response = session.get(url, params={"timeout": 1, "allowed_updates": ["message"]}, timeout=8)
        if response.status_code != 200:
            return 0
        data = response.json()
        updates = data.get("result", [])
        if not updates:
            return 0

        latest_update_id = None
        new_member_names = []

        for update in updates:
            latest_update_id = update.get("update_id")
            message = update.get("message", {})
            msg_chat = message.get("chat", {})
            if str(msg_chat.get("id")) != str(chat_id):
                continue
            new_users = message.get("new_chat_members", [])
            for user in new_users:
                if user.get("is_bot"):
                    continue
                name = user.get("first_name") or user.get("username") or "there"
                new_member_names.append(html.escape(name))

        if latest_update_id is not None:
            session.get(url, params={"offset": latest_update_id + 1, "timeout": 0}, timeout=5)

        if new_member_names:
            names_text = ", ".join(new_member_names)
            welcome_text = format_welcome_guide(greeting_name=names_text)
            success = send_telegram_message(bot_token, chat_id, welcome_text)
            if success:
                welcomed = len(new_member_names)
                print(f"Welcomed {welcomed} new member(s): {names_text}")

    except Exception as error:
        print(f"Warning: Failed to check for new members: {error}", file=sys.stderr)
    finally:
        session.close()

    return welcomed


# ---------------------------------------------------------------------------
# Cache & scan persistence
# ---------------------------------------------------------------------------


# Fields retained when caching quotes. Trimming keeps cached_odds.json small
# (was 130MB+ with full quotes) and avoids disk-I/O hangs on slow VPS disks.
_QUOTE_CACHE_FIELDS = frozenset({
    "sport", "event_id", "matchup", "kickoff", "is_live", "completed",
    "market", "market_key", "period", "team", "point", "expected_outcomes",
    "selection", "sportsbook", "bookmaker_key", "odds_american", "odds_decimal",
    "price_format", "liquidity", "liquidity_updated_at", "last_update",
    "last_change_at", "last_seen_at", "book_updated_at",
    "is_player_prop", "player_id", "player_name", "line_type",
})


def _trim_quote(quote: dict[str, Any]) -> dict[str, Any]:
    # Quotes parsed by ``propline_api.parse_odds_payload`` already contain only
    # these fields, so avoid copying 130k dicts per scan.
    if not quote.keys() - _QUOTE_CACHE_FIELDS:
        return quote
    return {key: quote.get(key) for key in _QUOTE_CACHE_FIELDS if key in quote}


def save_cached_odds(
    quotes: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    arbitrage: list[dict[str, Any]],
    sports: list[str],
    now_utc: datetime,
    path: str = CACHED_ODDS_FILE,
) -> None:
    data = {
        "cached_at": now_utc.isoformat(),
        "sports": sports,
        "quotes_count": len(quotes),
        "opportunities_count": len(opportunities),
        "arbitrage_count": len(arbitrage),
        "quotes": [_trim_quote(q) for q in quotes],
        "opportunities": opportunities,
        "arbitrage": arbitrage,
    }
    try:
        _atomic_write_json(path, data)
    except Exception as error:
        print(f"Error: Failed to save cached odds to {path}: {error}", file=sys.stderr)


def load_cached_odds(path: str = CACHED_ODDS_FILE) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else None
    except Exception as error:
        print(f"Warning: Failed to load {path}: {error}")
        return None


def _database_path() -> str:
    return os.getenv("BETS_DB_PATH", os.path.join("data", "bets.db"))


def persist_scan_to_database(
    observed_at: datetime,
    opportunities: list[dict[str, Any]],
    pinnacle_snapshots: list[dict[str, Any]],
) -> None:
    """
    Log non-player-prop +EV rows and Pinnacle snapshots to ``data/bets.db``.

    SQLite's transaction commit is atomic; ``database.log_scan`` wraps all
    inserts for the scan in one transaction. Player props are filtered here as
    a second line of defense.
    """
    eligible = [
        row for row in opportunities if not is_player_prop_quote(row)
    ]
    snapshots = [
        snapshot
        for snapshot in pinnacle_snapshots
        if not str(snapshot.get("market_key") or "")
        .split(":", 1)[0]
        .casefold()
        .startswith("player_")
    ]
    if not eligible and not snapshots:
        return
    try:
        import database

        database.DB_PATH = _database_path()
        database.log_scan(
            str(uuid4()),
            observed_at.isoformat(),
            eligible,
            snapshots,
        )
    except Exception as error:
        print(
            f"Warning: Could not log scan to {_database_path()}: {error}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Prompt/fetch helpers
# ---------------------------------------------------------------------------


def fetch_odds_with_fallback(
    sports: list[str],
    keys: list[str],
    mode: str = "upcoming",
    now: datetime | None = None,
    quota_state_file: str | None = None,
    failover_threshold: int = CONFIG.quota_failover_threshold,
) -> tuple[dict[str, Any], str]:
    """
    Fetch odds with automatic failover when the active key is low, rate limited,
    or rejected. One request is made per sport per key (no per-event polling).
    """
    if not keys:
        return {
            "quotes": [],
            "errors": ["No PropLine API key configured."],
            "warnings": [],
            "request_count": 0,
            "quota": {},
        }, ""

    last_result: dict[str, Any] = {"quotes": [], "errors": [], "warnings": [], "quota": {}}
    used_key = keys[0]

    for index, key in enumerate(keys):
        used_key = key
        key_label = f"Key #{index + 1}"
        # Check persisted quota before spending a request: rolling over after
        # a full scan doubled both latency and quota use.
        known_remaining = remaining_quota_for_key(key, quota_state_file)
        if (
            known_remaining is not None
            and known_remaining < failover_threshold
            and index + 1 < len(keys)
        ):
            print(
                f"Skipping PropLine {key_label}: {known_remaining} requests left "
                f"(below the {failover_threshold} failover threshold).",
                file=sys.stderr,
            )
            continue
        print(f"Fetching odds using PropLine {key_label}...")
        result = run_api(
            sports,
            mode=mode,
            api_key=key,
            now=now,
            quota_state_file=quota_state_file,
            failover_threshold=failover_threshold,
        )
        last_result = result

        errors = result.get("errors", [])
        has_quota_or_auth_error = any(
            ("429" in err or "quota" in err.lower() or "401" in err or "rejected" in err.lower())
            for err in errors
        )

        remaining = result.get("quota", {}).get("X-Daily-Remaining")
        try:
            is_low = remaining is not None and int(remaining) < failover_threshold
        except (TypeError, ValueError):
            is_low = False

        if quota_state_file:
            record_quota(key, result.get("quota", {}), quota_state_file, now)

        if (has_quota_or_auth_error or is_low) and (index + 1 < len(keys)):
            next_label = f"Key #{index + 2}"
            print(
                f"Warning: PropLine {key_label} is low or rate-limited "
                f"({remaining} remaining). Rolling over to {next_label}...",
                file=sys.stderr,
            )
            continue

        return result, used_key

    return last_result, used_key


def apply_arbitrage_size_limits(
    arb: dict[str, Any],
    max_leg_stake: float | None = None,
    max_total_stake: float | None = None,
    rounding_step: int = 1,
) -> bool:
    """Apply the per-leg and total-stake limits to an arbitrage card in place."""
    legs = arb.get("legs") or []
    if len(legs) != 2:
        return False
    odds = [float(leg.get("odds_decimal", 0.0)) for leg in legs]
    if any(price <= 1 for price in odds):
        return False
    requested_total = int(float(arb.get("total_stake", 0)))
    if max_total_stake is not None and requested_total > int(max_total_stake):
        requested_total = int(max_total_stake)
    sizing = size_arbitrage_stakes(
        odds,
        requested_total,
        rounding_step,
        max_leg_stake=max_leg_stake,
    )
    if sizing is None:
        return False
    total, stakes, profit, guaranteed_payout = sizing
    arb["total_stake"] = total
    arb["profit"] = profit
    arb["roi_pct"] = round(profit / total * 100, 2) if total else 0.0
    arb["locked_payout"] = round(guaranteed_payout, 2)
    arb["stake_reduced"] = total < requested_total
    for leg, stake in zip(legs, stakes):
        leg["stake"] = stake
        leg["payout"] = round(stake * float(leg["odds_decimal"]), 2)
    return True


def filter_eligible_arbs(
    raw_arbs: list[dict[str, Any]],
    now_utc: datetime,
    min_arb_roi: float,
    max_bet_cap: float,
    min_profit_dollars: float = ARB_MIN_PROFIT_DOLLARS,
) -> list[dict[str, Any]]:
    """Keep arbs that stay eligible after the stake caps are applied.

    ``find_arbitrage_opportunities`` sizes at a larger target stake, so
    re-capping the total to $300 lowers the guaranteed profit. An arb that
    drops below ``min_profit_dollars`` at the capped stake is not alertable and
    must not be cached.
    """
    valid: list[dict[str, Any]] = []
    for arb in raw_arbs:
        commence_time = arb.get("kickoff") or arb.get("commence_time")
        if is_game_started(
            commence_time, now_utc, CONFIG.prestart_guard_minutes
        ):
            continue
        if not apply_arbitrage_size_limits(
            arb,
            max_leg_stake=max_bet_cap,
            max_total_stake=ARB_MAX_TOTAL_STAKE_DOLLARS,
            rounding_step=1,
        ):
            continue
        if float(arb.get("roi_pct", 0.0)) < min_arb_roi:
            continue
        if float(arb.get("profit", 0.0)) < min_profit_dollars - 1e-9:
            continue
        if any(
            float(leg.get("stake", 0.0)) > max_bet_cap
            for leg in arb.get("legs", [])
        ):
            continue
        valid.append(arb)
    return valid


# ---------------------------------------------------------------------------
# Peer-to-peer exchange capacity
# ---------------------------------------------------------------------------


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def is_p2p_exchange(entry: dict[str, Any]) -> bool:
    """True for the exchanges whose fills must be verified before alerting."""
    key = str(entry.get("bookmaker_key") or "").strip().casefold()
    if key and key in CONFIG.p2p_exchange_bookmaker_keys:
        return True
    name = str(entry.get("sportsbook") or "").strip().casefold()
    return name in {"prophetx", "novig"}


def exchange_capacity_rejection(
    entry: dict[str, Any], required_stake: float
) -> str | None:
    """Reason to withhold an alert when an exchange cannot fill the bet.

    Returns ``None`` for non-exchange venues. Exchanges with no reported size,
    or whose available size at the quoted price is below the required stake
    times ``liquidity_buffer_multiplier``, are rejected so race conditions do
    not leave a bet unfilled (or a hedge leg unmatched).
    """
    if not is_p2p_exchange(entry):
        return None
    liquidity = _finite_positive(entry.get("liquidity"))
    stake = _finite_positive(required_stake)
    if liquidity is None:
        return "no liquidity reported at the target price"
    if stake is None or stake <= 0:
        return None
    multiplier = CONFIG.liquidity_buffer_multiplier
    allowed = liquidity * multiplier
    if stake > allowed + 1e-9:
        return (
            f"needs ${stake:,.2f} but only ${allowed:,.2f} is available "
            f"at the quoted price ({multiplier:.0%} of ${liquidity:,.2f})"
        )
    return None


def filter_exchange_capacity_ev(
    opportunities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop +EV exchange bets that cannot absorb their required Kelly stake."""
    kept = []
    for opportunity in opportunities:
        reason = exchange_capacity_rejection(
            opportunity, float(opportunity.get("required_stake") or 0.0)
        )
        if reason is not None:
            print(
                f"Skipping +EV {opportunity.get('sportsbook')} "
                f"{opportunity.get('matchup')}: {reason}."
            )
            continue
        kept.append(opportunity)
    return kept


def filter_exchange_capacity_arbs(
    arbs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop arbs whose exchange leg cannot be filled at the exact hedge stake."""
    kept = []
    for arb in arbs:
        rejected = False
        for leg in arb.get("legs", []):
            required = float(leg.get("stake") or leg.get("required_stake") or 0.0)
            reason = exchange_capacity_rejection(leg, required)
            if reason is not None:
                print(
                    f"Skipping arbitrage {arb.get('matchup')} "
                    f"({leg.get('sportsbook')} leg): {reason}."
                )
                rejected = True
                break
        if not rejected:
            for leg in arb.get("legs", []):
                leg["required_stake"] = float(leg.get("stake") or 0.0)
            kept.append(arb)
    return kept


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------


def in_ev_odds_range(opportunity: dict[str, Any]) -> bool:
    """True when the quoted American odds fall between -150 and +200 inclusive."""
    odds = opportunity.get("odds_american")
    if odds is None:
        return False
    try:
        value = float(odds)
    except (TypeError, ValueError):
        return False
    return EV_AMERICAN_MIN <= value <= EV_AMERICAN_MAX


def in_peak_window(now_utc: datetime) -> bool:
    """True during the alert window: 3-8 PM ET on weekdays, 9 AM-11 PM on weekends."""
    local = now_utc.astimezone(ET_ZONE)
    hour = local.hour
    if local.weekday() >= 5:  # Saturday or Sunday
        return WEEKEND_PEAK_START_HOUR_ET <= hour < WEEKEND_PEAK_END_HOUR_ET
    return PEAK_START_HOUR_ET <= hour < PEAK_END_HOUR_ET


def _eastern_date(now_utc: datetime) -> str:
    return now_utc.astimezone(ET_ZONE).date().isoformat()


def _daily_key(kind: str, now_utc: datetime) -> str:
    return f"__daily__|{kind}|{_eastern_date(now_utc)}"


def daily_count(history: dict[str, dict[str, Any]], kind: str, now_utc: datetime) -> int:
    entry = history.get(_daily_key(kind, now_utc), {})
    return int(entry.get("count", 0))


def increment_daily_count(
    history: dict[str, dict[str, Any]], kind: str, now_utc: datetime, now_ts: float
) -> None:
    key = _daily_key(kind, now_utc)
    history[key] = {"count": daily_count(history, kind, now_utc) + 1, "timestamp": now_ts}


def _is_spread_or_total(market: Any) -> bool:
    text = str(market or "").casefold()
    return "spread" in text or "total" in text or "over" in text or "o/u" in text


def _is_ncaab(sport: Any) -> bool:
    return str(sport or "").casefold() == "ncaab"


def is_cant_miss(market: Any, sport: Any) -> bool:
    """Spreads, totals and moneylines for NFL and NBA, plus their main player props."""
    if str(sport or "").casefold() not in CANT_MISS_SPORTS:
        return False
    text = str(market or "").casefold()
    return any(
        word in text
        for word in ("spread", "total", "moneyline", "h2h", "touchdown", "rushing", "receiving", "passing", "points", "rebounds", "assists")
    )


def _bet_group_key(opportunity: dict[str, Any]) -> str:
    """One key per bet across sportsbooks, so only the best book is alerted."""
    return "|".join(
        str(opportunity.get(field) or "")
        for field in ("event_id", "market", "selection", "line")
    )


def _ev_rank(opportunity: dict[str, Any]) -> float:
    """Rank by executable EV when known, otherwise by the quoted EV."""
    value = opportunity.get("executable_ev_pct")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return float(opportunity.get("ev_pct", 0.0) or 0.0)


def select_ev_alerts(
    candidates: list[dict[str, Any]],
    history: dict[str, dict[str, Any]],
    now_utc: datetime,
) -> list[dict[str, Any]]:
    """Best +EV bets only: peak window, one book per bet, daily cap."""
    if not in_peak_window(now_utc):
        return []
    remaining = DAILY_EV_ALERT_LIMIT - daily_count(history, "ev", now_utc)
    if remaining <= 0:
        return []
    best: dict[str, dict[str, Any]] = {}
    for opportunity in candidates:
        group = _bet_group_key(opportunity)
        current = best.get(group)
        if current is None or _ev_rank(opportunity) > _ev_rank(current):
            best[group] = opportunity
    ranked = sorted(best.values(), key=_ev_rank, reverse=True)
    chosen = []
    for opportunity in ranked:
        if len(chosen) >= remaining:
            break
        if should_alert(build_alert_key(opportunity), float(opportunity.get("ev_pct", 0.0)), history):
            chosen.append(opportunity)
    return chosen


def select_arb_alerts(
    candidates: list[dict[str, Any]],
    history: dict[str, dict[str, Any]],
    now_utc: datetime,
) -> list[dict[str, Any]]:
    """Arbs with ROI of at least 1% and profit of at least $10, no daily cap."""
    remaining = DAILY_ARB_ALERT_LIMIT - daily_count(history, "arb", now_utc)
    if remaining <= 0:
        return []
    eligible = []
    for arb in candidates:
        roi = float(arb.get("roi_pct", 0.0))
        profit = float(arb.get("profit", 0.0))
        if roi < ARB_ROI_MIN_PCT:
            continue
        if profit < ARB_MIN_PROFIT_DOLLARS - 1e-9:
            continue
        eligible.append(arb)
    best: dict[str, dict[str, Any]] = {}
    for arb in eligible:
        group = f"{arb.get('event_id')}|{arb.get('market')}"
        current = best.get(group)
        if current is None or float(arb.get("roi_pct", 0.0)) > float(current.get("roi_pct", 0.0)):
            best[group] = arb
    ranked = sorted(
        best.values(),
        key=lambda arb: (
            is_cant_miss(arb.get("market"), arb.get("sport")),
            float(arb.get("roi_pct", 0.0)),
        ),
        reverse=True,
    )
    chosen = []
    for arb in ranked:
        if len(chosen) >= remaining:
            break
        if should_alert_arbitrage(build_arbitrage_alert_key(arb), float(arb.get("roi_pct", 0.0)), history):
            chosen.append(arb)
    return chosen


def increment_daily_arb_counts(
    history: dict[str, dict[str, Any]], arb: dict[str, Any], now_utc: datetime, now_ts: float
) -> None:
    increment_daily_count(history, "arb", now_utc, now_ts)


def run_worker(
    sports: list[str] | None = None,
    min_ev: float | None = None,
    min_arb_roi: float = DEFAULT_MIN_ARB_ROI,
    max_bet_cap: float | None = None,
    api_key: str | None = None,
    api_keys: list[str] | None = None,
    history_file: str | None = None,
    cache_file: str | None = None,
    dry_run: bool = False,
    max_ev: float | None | object = ...,
    quota_state_file: str | None = None,
) -> dict[str, Any]:
    """Run one scan-and-alert cycle for +EV and arbitrage."""
    now_utc = datetime.now(timezone.utc)
    now_ts = time.time()

    # Self-timeout: prevent indefinite hangs (Unix only). 240s leaves margin
    # before the cron's 300s timeout.
    _alarm_set = False
    try:
        import signal

        def _timeout_handler(signum, frame):
            print("ERROR: Worker timed out after 240s; exiting.", file=sys.stderr)
            os._exit(1)

        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(240)
        _alarm_set = True
    except (AttributeError, ValueError, OSError):
        pass  # Windows or non-main thread; rely on cron timeout instead
    history_file = history_file or os.getenv(
        "ALERT_HISTORY_FILE", ALERT_HISTORY_FILE
    )
    cache_file = cache_file or os.getenv("CACHED_ODDS_FILE", CACHED_ODDS_FILE)
    quota_state_file = quota_state_file or _quota_state_path()
    max_bet_cap = (
        _get_float_env("MAX_SINGLE_BET", MAX_SINGLE_BET)
        if max_bet_cap is None
        else max_bet_cap
    )
    configured_min, configured_max = worker_ev_settings()
    if min_ev is None:
        min_ev = configured_min
    if max_ev is ...:
        max_ev = configured_max
    if max_ev is not None and max_ev < min_ev:
        max_ev = None

    config_errors = []
    if not math.isfinite(min_ev) or min_ev < 0:
        config_errors.append("WORKER_MIN_EV_PCT must be a finite number >= 0.")
    if max_ev is not None and (not math.isfinite(max_ev) or max_ev < min_ev):
        config_errors.append("WORKER_MAX_EV_PCT must be >= the minimum EV floor.")
    if not math.isfinite(min_arb_roi) or min_arb_roi < 0:
        config_errors.append("min_arb_roi must be >= 0.")
    if not math.isfinite(max_bet_cap) or max_bet_cap < 1:
        config_errors.append("MAX_SINGLE_BET must be >= 1.")
    if config_errors:
        for error in config_errors:
            print(f"CONFIG ERROR: {error}", file=sys.stderr)
        return {
            "success": False,
            "error": "; ".join(config_errors),
            "ev_alerts_sent": 0,
            "arb_alerts_sent": 0,
        }

    cached_payload = load_cached_odds(cache_file)
    if sports is not None:
        scan_sports = list(sports)
    else:
        # Dynamic active-sport selection (cached 6h; ~6 requests/day, quota-counted).
        # Only Tier 1 auto-include sports are ever scanned, and only when they
        # have an upcoming game within SPORT_LOOKAHEAD_DAYS. This keeps thin/obscure
        # markets (and their limit/ban risk) out of the scan automatically.
        events_cache = _load_cached_json("sport_events_cache.json", 21600)
        events_by_sport: dict[str, list[dict[str, Any]]] = {}
        if events_cache is None:
            api_key_for_events = (keys_to_use[0] if "keys_to_use" in locals() and keys_to_use else None) or os.getenv(
                "PROPLINE_API_KEY_1", os.getenv("PROPLINE_API_KEY", "")
            )
            for sport in ACTIVE_SPORTS:
                if sport not in AUTO_INCLUDE_SPORTS:
                    continue
                events_by_sport[sport] = fetch_sport_events(sport, api_key_for_events)
            _save_cached_json("sport_events_cache.json", {"events": events_by_sport})
        else:
            events_by_sport = events_cache.get("events", {}) or {}
        now_for_events = datetime.now(timezone.utc)
        scan_sports = []
        for sport in ACTIVE_SPORTS:
            if sport not in AUTO_INCLUDE_SPORTS:
                continue
            if sport_has_event_in_window(
                sport, events_by_sport.get(sport, []) or [], now_for_events
            ):
                scan_sports.append(sport)
            else:
                print(
                    f"Skipping {sport}: no event within {scan_lead_hours(sport)}h "
                    "of now (outside its scan window)."
                )
        if not scan_sports:
            scan_sports = select_scan_sports(
                [s for s in ACTIVE_SPORTS if s in AUTO_INCLUDE_SPORTS], cached_payload
            )

    # Validate scan_sports against SPORT_CONFIG.
    invalid = [s for s in scan_sports if s not in SPORT_CONFIG]
    if invalid:
        print(f"CONFIG ERROR: Unknown sports in scan list: {', '.join(invalid)}", file=sys.stderr)
        return {
            "success": False,
            "error": f"Unknown sports: {', '.join(invalid)}",
            "ev_alerts_sent": 0,
            "arb_alerts_sent": 0,
        }

    if api_keys:
        keys_to_use = [key for key in api_keys if key and key.strip()]
    elif api_key:
        keys_to_use = [api_key]
    else:
        keys_to_use = get_api_keys(now_utc)

    print(f"[{now_utc.isoformat()}] Starting +EV & Arbitrage cron worker...")
    print(f"Active sports: {', '.join(scan_sports) or 'none'}")
    print(
        f"Minimum EV threshold: {min_ev:.1f}% | "
        f"Max EV cap: {'none' if max_ev is None else f'{max_ev:.1f}%'} | "
        f"Min Arb ROI: {min_arb_roi:.1f}% | Max Bet Cap: ${max_bet_cap:.0f}"
    )
    print(f"Configured PropLine API keys available: {len(keys_to_use)}")

    bot_token, chat_id = get_telegram_credentials()
    telegram_configured = bool(bot_token and chat_id)
    discord_configured = bool(get_discord_webhook())
    alerts_configured = telegram_configured or discord_configured
    if not alerts_configured:
        print(
            "WARNING: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID and DISCORD_WEBHOOK_URL "
            "are not configured. Alerts will be logged to the console only."
        )

    # New-member onboarding is handled by a pinned guide message (see --send-guide),
    # not by per-scan welcome alerts. This avoids wasteful getUpdates calls and
    # alert spam on every cron run.

    if not keys_to_use:
        print(
            "ERROR: PropLine API key is missing. Set PROPLINE_API_KEY_1 and "
            "PROPLINE_API_KEY_2 (or PROPLINE_API_KEY).",
            file=sys.stderr,
        )
        return {
            "success": False,
            "error": "Missing PropLine API key",
            "ev_alerts_sent": 0,
            "arb_alerts_sent": 0,
        }

    if not scan_sports:
        print("No active sports to scan; skipping this cycle.")
        return {
            "success": True,
            "quotes_fetched": 0,
            "eligible_opportunities": 0,
            "eligible_arbitrages": 0,
            "ev_alerts_sent": 0,
            "arb_alerts_sent": 0,
            "tracked_bets": 0,
            "sports_skipped": True,
        }

    # Free-tier freshness (no auth, not quota-counted): fetch in parallel with odds.
    freshness_cache = os.getenv("FRESHNESS_CACHE_FILE", "freshness_cache.json")

    # Free-tier injuries: start fetching in the background so they overlap with
    # the odds fetch. Cached 1h to protect quota.
    skip_statuses = {
        status.strip().lower()
        for status in os.getenv(
            "WORKER_SKIP_INJURY_STATUSES", "out,doubtful,questionable"
        ).split(",")
        if status.strip()
    }
    # Fetch odds (the slow part).
    fetch_result, active_key = fetch_odds_with_fallback(
        scan_sports,
        keys_to_use,
        mode="upcoming",
        now=now_utc,
        quota_state_file=quota_state_file,
    )
    quotes = fetch_result.get("quotes", [])
    errors = fetch_result.get("errors", [])
    warnings = fetch_result.get("warnings", [])

    for error in errors:
        print(f"API Error: {error}", file=sys.stderr)
    for warning in warnings:
        print(f"API Warning: {warning}")

    print(f"Fetched {len(quotes)} quotes across {len(scan_sports)} sports.")

    # Resolve freshness (cached 5 min).
    freshness_payload = _load_cached_json(freshness_cache, 300)
    if freshness_payload is None:
        freshness_payload = {"data": fetch_freshness()}
        _save_cached_json(freshness_cache, freshness_payload)
    freshness_data = (
        freshness_payload.get("data", {})
        if isinstance(freshness_payload, dict)
        else {}
    )
    stale_books = stale_book_keys(freshness_data)
    if stale_books:
        before = len(quotes)
        quotes = [
            quote
            for quote in quotes
            if str(quote.get("bookmaker_key") or "").strip().lower()
            not in stale_books
        ]
        print(
            f"Freshness: dropped {before - len(quotes)} quotes from stale books "
            f"({', '.join(sorted(stale_books))})."
        )

    # Resolve injuries (wait for background fetch if still running).
    injured_names: set[str] = set()
    if skip_statuses:
        from concurrent.futures import ThreadPoolExecutor as _TPE

        def _fetch_one_injury(sport: str) -> list[dict[str, Any]]:
            cache_path = f"injuries_cache_{sport}.json"
            cached = _load_cached_json(cache_path, 3600)
            if cached is None:
                players = fetch_injuries(sport, keys_to_use[0] if keys_to_use else "")
                _save_cached_json(cache_path, {"players": players})
            else:
                players = cached.get("players", [])
            return players

        pool = _TPE(max_workers=3)
        try:
            for players in pool.map(_fetch_one_injury, list(scan_sports)):
                injured_names.update(injured_player_names(players, skip_statuses))
        finally:
            pool.shutdown(wait=False)
        if injured_names:
            before = len(quotes)
            quotes = [
                quote
                for quote in quotes
                if not (
                    is_player_prop_quote(quote)
                    and _normalize_player_name(quote.get("player_name"))
                    in injured_names
                )
            ]
            print(
                f"Injuries: dropped {before - len(quotes)} prop quotes for "
                f"{len(injured_names)} injured/questionable players."
            )

    # +EV is Pinnacle-anchored and excludes player props by construction.
    # The worker applies its own $10 minimum, so the engine's floor is lifted here.
    scanner_cfg = replace(CONFIG, min_ev_pct=min_ev, min_arb_guaranteed_profit_dollars=0.0)
    scan_res = scan_pinnacle_ev(quotes, config=scanner_cfg, now=now_utc)
    raw_opportunities = [
        opportunity
        for opportunity in scan_res.get("opportunities", [])
        if not is_player_prop_quote(opportunity)
    ]

    valid_opportunities = []
    for opportunity in raw_opportunities:
        ev = float(opportunity.get("ev_pct", 0.0))
        if ev < min_ev:
            continue
        if max_ev is not None and ev > max_ev:
            continue
        if not in_ev_odds_range(opportunity):
            continue
        commence_time = opportunity.get("kickoff") or opportunity.get("commence_time")
        if is_game_started(
            commence_time, now_utc, CONFIG.prestart_guard_minutes
        ):
            continue
        valid_opportunities.append(opportunity)

    if valid_opportunities:
        required_stakes = calculate_required_kelly_stakes(
            valid_opportunities,
            bankroll=CONFIG.bankroll,
            fraction=CONFIG.kelly_fraction,
            # Pure Kelly (not the 1% display cap), bounded only by the $300
            # single-bet limit, so the liquidity check cannot be circular.
            per_bet_cap_fraction=1.0,
            per_event_cap_fraction=1.0,
            max_single_bet=max_bet_cap,
        )
        scan_stakes = calculate_capped_kelly_stakes(
            valid_opportunities,
            bankroll=CONFIG.bankroll,
            fraction=CONFIG.kelly_fraction,
        )
        for opportunity, stake, required in zip(
            valid_opportunities, scan_stakes, required_stakes
        ):
            opportunity["kelly_stake"] = min(float(stake), max_bet_cap)
            opportunity["required_stake"] = float(required)
            opportunity["min_bet_amount"] = float(required)
        valid_opportunities = filter_exchange_capacity_ev(valid_opportunities)

    print(
        f"Found {len(valid_opportunities)} eligible +EV opportunities "
        f"(>= {min_ev:.1f}% EV, max bet=${max_bet_cap:.0f})."
    )

    # Arbitrage: Pinnacle is excluded inside the engine and only opportunities
    # that still guarantee the minimum profit after the $300 stake cap is
    # applied are kept.
    raw_arbs = find_arbitrage_opportunities(
        quotes,
        total_stake=ARBITRAGE_TARGET_TOTAL_STAKE,
        rounding_step=1,
        config=scanner_cfg,
        now=now_utc,
    )
    valid_arbs = filter_eligible_arbs(
        raw_arbs,
        now_utc=now_utc,
        min_arb_roi=min_arb_roi,
        max_bet_cap=max_bet_cap,
    )
    valid_arbs = filter_exchange_capacity_arbs(valid_arbs)

    print(
        f"Found {len(valid_arbs)} eligible Arbitrage opportunities "
        f"(ROI >= {min_arb_roi:.1f}%, max bet=${max_bet_cap:.0f}, "
        f"min profit=${ARB_MIN_PROFIT_DOLLARS:.2f})."
    )

    # Cache is written atomically (.tmp + os.replace).
    save_cached_odds(
        quotes, valid_opportunities, valid_arbs, scan_sports, now_utc, cache_file
    )
    print(
        f"Saved {len(quotes)} quotes, {len(valid_opportunities)} EV opps, and "
        f"{len(valid_arbs)} arbs to {cache_file}."
    )

    snapshots = scan_res.get("pinnacle_snapshots")
    if snapshots is not None and not dry_run:
        persist_scan_to_database(
            now_utc,
            scan_res.get("eligible_rows", valid_opportunities),
            snapshots,
        )

    history = load_alert_history(history_file)
    initial_history_count = len(history)
    history = clean_expired_history(history, now_ts, MAX_HISTORY_AGE_SECONDS)
    pruned = initial_history_count - len(history)
    if pruned > 0:
        print(f"Pruned {pruned} alert history entries older than 48 hours.")

    ev_alerts_sent = 0
    for opportunity in select_ev_alerts(valid_opportunities, history, now_utc)[
        :MAX_EV_ALERTS_PER_SCAN
    ]:
        alert_key = build_alert_key(opportunity)
        ev = float(opportunity.get("ev_pct", 0.0))

        message = format_telegram_alert(opportunity)
        if alerts_configured and not dry_run:
            print(f"Sending +EV alert for {alert_key} (+{ev:.2f}% EV)...")
            if dispatch_alert(message):
                ev_alerts_sent += 1
                history[alert_key] = {"ev": ev, "timestamp": now_ts}
                increment_daily_count(history, "ev", now_utc, now_ts)
                if ALERT_SEND_DELAY_SECONDS > 0:
                    time.sleep(ALERT_SEND_DELAY_SECONDS)
            else:
                print(f"[FALLBACK +EV ALERT] {alert_key}:\n{message}\n")
        else:
            # Dry run (or no Telegram configured): report only. Nothing is
            # recorded in the dedupe history, otherwise a test run would
            # silently suppress the real alert for the same opportunity.
            print(f"[CONSOLE +EV ALERT] {alert_key} (+{ev:.2f}% EV):\n{message}\n")
            ev_alerts_sent += 1

    arb_alerts_sent = 0
    for arb in select_arb_alerts(valid_arbs, history, now_utc)[
        :MAX_ARB_ALERTS_PER_SCAN
    ]:
        arb_key = build_arbitrage_alert_key(arb)
        roi = float(arb.get("roi_pct", 0.0))

        message = format_telegram_arbitrage_alert(arb)
        if alerts_configured and not dry_run:
            print(f"Sending Arbitrage alert for {arb_key} (+{roi:.2f}% ROI)...")
            if dispatch_alert(message):
                arb_alerts_sent += 1
                history[arb_key] = {"roi": roi, "timestamp": now_ts}
                increment_daily_arb_counts(history, arb, now_utc, now_ts)
                if ALERT_SEND_DELAY_SECONDS > 0:
                    time.sleep(ALERT_SEND_DELAY_SECONDS)
            else:
                print(f"[FALLBACK ARB ALERT] {arb_key}:\n{message}\n")
        else:
            # See the +EV branch: dry runs must not poison the dedupe history.
            print(f"[CONSOLE ARB ALERT] {arb_key} (+{roi:.2f}% ROI):\n{message}\n")
            arb_alerts_sent += 1

    if not dry_run:
        save_alert_history(history, history_file)
    print(
        f"Alert cycle complete. +EV alerts: {ev_alerts_sent}, "
        f"Arb alerts: {arb_alerts_sent}. Total active tracked items: {len(history)}."
    )

    try:
        import signal

        signal.alarm(0)
    except (AttributeError, ValueError, OSError):
        pass

    return {
        "success": True,
        "quotes_fetched": len(quotes),
        "eligible_opportunities": len(valid_opportunities),
        "eligible_arbitrages": len(valid_arbs),
        "ev_alerts_sent": ev_alerts_sent,
        "arb_alerts_sent": arb_alerts_sent,
        "tracked_bets": len(history),
        "request_count": fetch_result.get("request_count", 0),
        "quota": fetch_result.get("quota", {}),
        "api_key_used_index": keys_to_use.index(active_key) + 1
        if active_key in keys_to_use
        else None,
        "failover_used": fetch_result.get("failover_used", False),
    }


def main() -> None:
    if "--send-guide" in sys.argv or "--welcome" in sys.argv:
        bot_token, chat_id = get_telegram_credentials()
        if not bot_token or not chat_id:
            print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.", file=sys.stderr)
            sys.exit(1)
        guide_text = format_welcome_guide()
        if send_telegram_message(bot_token, chat_id, guide_text):
            print("Successfully sent onboarding guide to Telegram group.")
        else:
            print("Failed to send onboarding guide to Telegram group.", file=sys.stderr)
            sys.exit(1)
        return

    # --announce "message" sends a one-off announcement to the Telegram group.
    announce_idx = None
    for i, arg in enumerate(sys.argv):
        if arg == "--announce":
            announce_idx = i
            break
    if announce_idx is not None and announce_idx + 1 < len(sys.argv):
        bot_token, chat_id = get_telegram_credentials()
        if not bot_token or not chat_id:
            print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.", file=sys.stderr)
            sys.exit(1)
        message = sys.argv[announce_idx + 1]
        if send_telegram_message(bot_token, chat_id, message):
            print("Announcement sent.")
        else:
            print("Failed to send announcement.", file=sys.stderr)
            sys.exit(1)
        return

    dry_run = "--dry-run" in sys.argv
    timeout_seconds = None
    for arg in sys.argv:
        if arg.startswith("--timeout="):
            try:
                timeout_seconds = int(arg.split("=", 1)[1])
            except ValueError:
                pass
    if timeout_seconds is None:
        env_timeout = os.getenv("WORKER_TIMEOUT_SECONDS", "").strip()
        if env_timeout:
            try:
                timeout_seconds = int(env_timeout)
            except ValueError:
                pass
    if timeout_seconds is not None:
        # Apply a hard timeout to the entire process.
        try:
            import signal

            def _hard_timeout(signum, frame):
                print(f"ERROR: Worker hard timeout ({timeout_seconds}s) reached.", file=sys.stderr)
                os._exit(1)

            signal.signal(signal.SIGALRM, _hard_timeout)
            signal.alarm(timeout_seconds)
        except (AttributeError, ValueError, OSError):
            pass
    result = run_worker(dry_run=dry_run)
    if not result.get("success", False):
        sys.exit(1)
    # Force immediate exit to avoid D-state hangs during Python cleanup
    # (HTTP connection pool teardown, GC on 130MB+ cached odds, etc.).
    os._exit(0)
    if not result.get("success", False):
        sys.exit(1)


if __name__ == "__main__":
    main()
