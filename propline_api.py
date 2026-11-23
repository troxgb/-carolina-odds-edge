import hashlib
import json
import math
import os
import socket
import threading

# Bound all socket operations (including DNS resolution) globally.
socket.setdefaulttimeout(10)
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NameResolutionError,
    NewConnectionError,
)
from urllib3.util import connection as urllib3_connection

from scanner_config import CONFIG

API_BASE_URL = "https://api.prop-line.com/v1"
GAME_MARKETS = ("h2h", "spreads", "totals")
API_MARKETS = ",".join(GAME_MARKETS)
EASTERN = ZoneInfo("America/New_York")

QUOTA_HEADERS = (
    "X-Daily-Remaining",
    "X-Daily-Used",
    "X-Daily-Reset",
    "X-Daily-Limit",
)

PLAYER_PROP_LABELS = {
    "player_points": "Player Points",
    "player_rebounds": "Player Rebounds",
    "player_assists": "Player Assists",
    "player_pass_yds": "Passing Yards",
    "player_rush_yds": "Rushing Yards",
    "player_reception_yds": "Receiving Yards",
}

_QUOTA_LOCK = threading.Lock()


class _IPv4HTTPSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:
        try:
            address = socket.getaddrinfo(
                self._dns_host, self.port, socket.AF_INET, socket.SOCK_STREAM
            )[0][4][0]
            return urllib3_connection.create_connection(
                (address, self.port),
                self.timeout,
                source_address=self.source_address,
                socket_options=self.socket_options,
            )
        except socket.gaierror as error:
            raise NameResolutionError(self.host, self, error) from error
        except socket.timeout as error:
            raise ConnectTimeoutError(
                self, f"Connection to {self.host} timed out."
            ) from error
        except OSError as error:
            raise NewConnectionError(
                self, f"Failed to establish an IPv4 connection: {error}"
            ) from error


class _IPv4HTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _IPv4HTTPSConnection


class _PropLineIPv4Adapter(HTTPAdapter):
    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any
    ) -> None:
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(
            self.poolmanager.pool_classes_by_scheme
        )
        self.poolmanager.pool_classes_by_scheme["https"] = _IPv4HTTPSConnectionPool


def _new_api_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://api.prop-line.com/", _PropLineIPv4Adapter())
    return session


SPORT_CONFIG = {
    "NFL": "football_nfl",
    "NCAAF": "football_ncaaf",
    "NBA": "basketball_nba",
    "NCAAB": "basketball_ncaab",
    "MLB": "baseball_mlb",
    "NHL": "hockey_nhl",
}

APPROVED_BOOKMAKERS = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "fanatics": "Fanatics",
    "thescorebet": "theScore Bet",
    "novig": "Novig",
    "prophetx": "ProphetX",
}


# ---------------------------------------------------------------------------
# API key rotation & quota state
# ---------------------------------------------------------------------------


def _key_fingerprint(api_key: str) -> str:
    """Stable, non-reversible identifier so raw keys are never persisted."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def load_api_key_pair() -> tuple[str, str]:
    """Load the two rotating PropLine keys, falling back to the legacy key."""
    key_1 = os.getenv("PROPLINE_API_KEY_1", "").strip()
    key_2 = os.getenv("PROPLINE_API_KEY_2", "").strip()
    legacy = os.getenv("PROPLINE_API_KEY", "").strip()

    if not key_1:
        key_1 = legacy
    if not key_2 and legacy and legacy != key_1:
        key_2 = legacy
    return key_1, key_2


def get_rotating_api_keys(now: datetime | None = None) -> list[str]:
    """
    Return the configured keys with the UTC half-day primary first.

    PropLine resets at 00:00 UTC. Key 1 is primary from 00:00-11:59 UTC and
    Key 2 is primary from 12:00-23:59 UTC.
    """
    key_1, key_2 = load_api_key_pair()
    keys = [key for key in (key_1, key_2) if key]
    if len(keys) < 2:
        return keys
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    hour = current.astimezone(timezone.utc).hour
    return [key_2, key_1] if hour >= 12 else [key_1, key_2]


def select_primary_api_key(now: datetime | None = None) -> str:
    keys = get_rotating_api_keys(now)
    return keys[0] if keys else ""


def parse_quota_headers(headers: Any) -> dict[str, str]:
    """Extract the daily quota headers PropLine returns on every response."""
    if not headers:
        return {}
    quota: dict[str, str] = {}
    for header in QUOTA_HEADERS:
        try:
            value = headers.get(header)
        except AttributeError:
            value = None
        if value is not None:
            quota[header] = str(value)
    return quota


def _quota_state_path(path: str | None = None) -> str:
    return path or CONFIG.quota_state_file


def load_quota_state(path: str | None = None) -> dict[str, Any]:
    state_path = _quota_state_path(path)
    if not os.path.exists(state_path):
        return {"keys": {}}
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, ValueError):
        return {"keys": {}}
    if not isinstance(state, dict) or not isinstance(state.get("keys"), dict):
        return {"keys": {}}
    return state


def save_quota_state(state: dict[str, Any], path: str | None = None) -> None:
    """Atomically persist quota state (write .tmp, then os.replace)."""
    state_path = _quota_state_path(path)
    directory = os.path.dirname(os.path.abspath(state_path))
    os.makedirs(directory, exist_ok=True)
    temporary_path = f"{state_path}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, indent=2)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, state_path)
    except OSError:
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass
        raise


def record_quota(
    api_key: str,
    quota: dict[str, str] | None,
    path: str | None = None,
    now: datetime | None = None,
) -> None:
    """Persist the latest observed quota headers for one key."""
    if not api_key or not quota:
        return
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    fingerprint = _key_fingerprint(api_key)
    with _QUOTA_LOCK:
        state = load_quota_state(path)
        entry = state.setdefault("keys", {}).setdefault(fingerprint, {})
        entry["headers"] = dict(quota)
        entry["updated_at"] = current.astimezone(timezone.utc).isoformat()
        if "X-Daily-Remaining" in quota:
            try:
                entry["remaining"] = int(quota["X-Daily-Remaining"])
            except (TypeError, ValueError):
                pass
        if "X-Daily-Used" in quota:
            try:
                entry["used"] = int(quota["X-Daily-Used"])
            except (TypeError, ValueError):
                pass
        if "X-Daily-Reset" in quota:
            entry["reset"] = quota["X-Daily-Reset"]
        if "X-Daily-Limit" in quota:
            try:
                entry["limit"] = int(quota["X-Daily-Limit"])
            except (TypeError, ValueError):
                pass
        try:
            save_quota_state(state, path)
        except OSError:
            # Quota bookkeeping must never take down a scan.
            pass


def _quota_window_expired(entry: dict[str, Any], now: datetime) -> bool:
    """True when the cached count belongs to a daily window that has rolled over."""
    reset = entry.get("reset")
    if reset in (None, ""):
        return False
    try:
        reset_ts = float(reset)
    except (TypeError, ValueError):
        return False
    return now.timestamp() >= reset_ts


def remaining_quota_for_key(
    api_key: str, path: str | None = None, now: datetime | None = None
) -> int | None:
    if not api_key:
        return None
    entry = load_quota_state(path).get("keys", {}).get(_key_fingerprint(api_key))
    if not isinstance(entry, dict):
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    # PropLine resets at 00:00 UTC. A count cached before that reset must not
    # keep a key benched: it would waste the whole fresh window while burning
    # the other key. Unknown means "usable"; the next response refreshes it.
    if _quota_window_expired(entry, current):
        return None
    try:
        return int(entry.get("remaining"))
    except (TypeError, ValueError):
        return None


def select_api_keys(
    now: datetime | None = None,
    quota_state_file: str | None = None,
    failover_threshold: int = CONFIG.quota_failover_threshold,
) -> list[str]:
    """
    Return rotating keys with the secondary promoted when the primary is low.

    The persisted state is checked before a scan, so an active key below the
    failover threshold never becomes the primary for that scan.
    """
    keys = get_rotating_api_keys(now)
    if len(keys) < 2:
        return keys
    remaining = remaining_quota_for_key(keys[0], quota_state_file)
    if remaining is not None and remaining < failover_threshold:
        return [keys[1], keys[0]]
    return keys


# ---------------------------------------------------------------------------
# Market helpers
# ---------------------------------------------------------------------------


def sport_markets(sport_label: str) -> str:
    """Bulk market list for a sport, including configured player props."""
    sport_key = SPORT_CONFIG[sport_label]
    player_markets = CONFIG.player_prop_markets_for_sport(sport_key)
    return ",".join([*GAME_MARKETS, *player_markets])


def is_player_prop_market(market_key: Any) -> bool:
    return str(market_key or "").split(":", 1)[0].casefold().startswith("player_")


def _supported_market(market_key: Any) -> bool:
    if not isinstance(market_key, str):
        return False
    base = market_key.split(":", 1)[0].casefold()
    return base in GAME_MARKETS or base.startswith("player_")


# The free tier returns every alternate and milestone line for a market. Only
# "main" lines (or rows with no line_type) are kept: alternates made up ~94% of
# returned quotes (one NCAAF request was 41 MB / ~83k quotes) and alternate
# points are rarely comparable across books, so they only cost RAM and swap.
_KEPT_LINE_TYPES = frozenset({"main"})


def _is_main_line(market: dict[str, Any]) -> bool:
    if not CONFIG.main_lines_only:
        return True
    line_type = market.get("line_type")
    if line_type is None:
        return True
    return str(line_type).strip().casefold() in _KEPT_LINE_TYPES


def _market_is_suspended(market: dict[str, Any]) -> bool:
    """True when the book pulled this market pregame.

    PropLine keeps returning the last quoted legs with ``suspended_at`` set
    (late scratch, dropped market type). Those legs are not a live price, so
    they must never become an +EV or arbitrage leg.
    """
    return bool(market.get("suspended_at"))


def _book_is_pregame_only(bookmaker: dict[str, Any]) -> bool:
    """True when the event is live and this book no longer prices it in play."""
    return bookmaker.get("pregame_only") is True


def _configured_prop_markets(sport_label: str) -> frozenset[str]:
    sport_key = SPORT_CONFIG.get(sport_label)
    if not sport_key:
        return frozenset()
    return frozenset(
        str(market).casefold()
        for market in CONFIG.player_prop_markets_for_sport(sport_key)
    )


def _normalize_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    import unicodedata

    text = " ".join(value.split())
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    parts = text.split()
    suffixes = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}
    while parts and parts[-1].casefold().rstrip(".") in {s.rstrip(".") for s in suffixes}:
        parts.pop()
    return " ".join(parts).casefold()


def _player_scope(player_id: Any, player_name: Any) -> str:
    if player_id is not None and str(player_id).strip():
        return f"id:{str(player_id).strip()}"
    normalized = _normalize_name(player_name)
    return f"name:{normalized}" if normalized else ""


def _player_outcome(
    market: dict[str, Any], outcome: dict[str, Any]
) -> tuple[str | None, Any, str | None]:
    """
    Return (selection, player_id, player_name) for a player prop outcome.

    PropLine (and Odds API-compatible feeds) usually put Over/Under in
    ``name`` and the player in ``description``. Some providers reverse those
    fields, so both shapes are normalized here.
    """
    player_id = outcome.get("player_id") or market.get("player_id")
    raw_name = outcome.get("name")
    raw_description = outcome.get("description") or outcome.get("player_name")

    name_text = str(raw_name).strip() if raw_name is not None else ""
    description_text = (
        str(raw_description).strip() if raw_description is not None else ""
    )
    over_under = {"over": "Over", "under": "Under"}

    if name_text.casefold() in over_under:
        selection = over_under[name_text.casefold()]
        player_name = description_text or None
    elif description_text.casefold() in over_under:
        selection = over_under[description_text.casefold()]
        player_name = name_text or None
    else:
        selection = name_text or None
        player_name = description_text or None

    if player_id is not None and not str(player_id).strip():
        player_id = None
    return selection, player_id, player_name


def american_to_decimal(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    price = float(value)
    if not math.isfinite(price) or price == 0:
        return None
    return 1 + (price / 100 if price > 0 else 100 / abs(price))


def _market_team(market: dict[str, Any], outcome: dict[str, Any]) -> str | None:
    team = market.get("team")
    if not isinstance(team, str) or not team.strip():
        team = outcome.get("team")
    if not isinstance(team, str) or not team.strip():
        return None
    return team.strip()


def _market_identity(
    market_key: str,
    point: Any,
    team: str | None = None,
    player: str | None = None,
    *,
    signed: bool = False,
) -> str:
    if point is None:
        identity = market_key
    else:
        try:
            number = float(point)
        except (TypeError, ValueError):
            number = math.nan
        if not math.isfinite(number):
            identity = market_key
        else:
            # Spreads are grouped by absolute line so the two sides of the same
            # number (e.g. Home +4.5 and Away -4.5) live in one market. The
            # signed form is used for per-book update tracking and deduplication
            # so a book's alternate lines for the same team at +X and -X do not
            # overwrite each other.
            if market_key == "spreads" and not signed:
                number = abs(number)
            identity = f"{market_key}:{number:g}"
    if market_key == "totals" and team:
        identity += f":team:{team.casefold()}"
    if player:
        identity += f":player:{player}"
    return identity


def _market_label(
    market_key: str,
    point: Any,
    team: str | None = None,
    player_name: str | None = None,
) -> str:
    if market_key.startswith("player_"):
        return PLAYER_PROP_LABELS.get(
            market_key, market_key.replace("_", " ").title()
        )

    if point is None:
        label = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}.get(
            market_key, market_key.replace("_", " ").title()
        )
    else:
        try:
            line = float(point)
        except (TypeError, ValueError):
            line = math.nan
        if not math.isfinite(line):
            label = market_key.replace("_", " ").title()
        elif market_key == "spreads":
            label = f"Spread {line:+g}"
        elif market_key == "totals":
            label = f"Total {line:g}"
        else:
            label = market_key.replace("_", " ").title()
    if market_key == "totals" and team:
        label += f" ({team} team)"
    return label


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _is_certificate_validation_error(error: requests.exceptions.SSLError) -> bool:
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "certificate verify failed",
            "hostname mismatch",
            "self-signed certificate",
            "unable to get local issuer certificate",
        )
    )


def _event_is_in_mode(event: dict[str, Any], mode: str, now: datetime) -> bool:
    is_live = event.get("live") is True
    if event.get("completed") is True:
        return False
    if mode == "live":
        return is_live
    if is_live:
        return False
    if now.tzinfo is None:
        return False
    commence_time = event.get("commence_time")
    if not isinstance(commence_time, str):
        return False
    try:
        commence = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        if commence.tzinfo is None:
            return False
        return commence > now
    except ValueError:
        return False


def parse_odds_payload(
    events: list[dict[str, Any]], sport_label: str
) -> list[dict[str, Any]]:
    quotes_by_identity: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    accepted_bookmakers = set(APPROVED_BOOKMAKERS) | {
        CONFIG.pinnacle_bookmaker_key
    }
    allowed_prop_markets = _configured_prop_markets(sport_label)
    for event in events:
        event_id = event.get("id")
        home = event.get("home_team")
        away = event.get("away_team")
        if (
            event_id is None
            or not isinstance(home, str)
            or not home.strip()
            or not isinstance(away, str)
            or not away.strip()
        ):
            continue

        bookmaker_rows = event.get("bookmakers")
        if not isinstance(bookmaker_rows, list):
            continue

        event_markets: dict[tuple[str, str, str], set[str]] = {}
        latest_market_updates: dict[tuple[str, str, str], datetime] = {}
        for bookmaker in bookmaker_rows:
            if (
                not isinstance(bookmaker, dict)
                or str(bookmaker.get("key", "")).casefold() not in accepted_bookmakers
                or _book_is_pregame_only(bookmaker)
            ):
                continue
            bookmaker_key = str(bookmaker["key"]).casefold()
            bookmaker_markets = bookmaker.get("markets")
            if not isinstance(bookmaker_markets, list):
                continue
            for market in bookmaker_markets:
                if not isinstance(market, dict):
                    continue
                market_key = market.get("key")
                if not _supported_market(market_key):
                    continue
                if not _is_main_line(market):
                    continue
                if _market_is_suspended(market):
                    continue
                if is_player_prop_market(market_key) and (
                    str(market_key).split(":", 1)[0].casefold()
                    not in allowed_prop_markets
                ):
                    continue
                player_prop = is_player_prop_market(market_key)
                for outcome in market.get("outcomes", []):
                    if not isinstance(outcome, dict):
                        continue
                    if player_prop:
                        selection, player_id, player_name = _player_outcome(
                            market, outcome
                        )
                        if not selection:
                            continue
                        player_scope = _player_scope(player_id, player_name)
                        group_key = _market_identity(
                            market_key, outcome.get("point"), None, player_scope
                        )
                        instance_key = group_key
                    else:
                        team = _market_team(market, outcome)
                        selection = str(outcome.get("name", ""))
                        group_key = _market_identity(
                            market_key, outcome.get("point"), team
                        )
                        instance_key = _market_identity(
                            market_key, outcome.get("point"), team, signed=True
                        )
                    period = str(market.get("period") or "")
                    event_markets.setdefault(
                        (bookmaker_key, instance_key, period), set()
                    ).add(str(selection))
                    update = _parse_timestamp(market.get("last_update"))
                    if update is not None:
                        update_key = (bookmaker_key, instance_key, period)
                        latest_market_updates[update_key] = max(
                            update, latest_market_updates.get(update_key, update)
                        )

        for bookmaker in bookmaker_rows:
            if (
                not isinstance(bookmaker, dict)
                or str(bookmaker.get("key", "")).casefold() not in accepted_bookmakers
                or _book_is_pregame_only(bookmaker)
            ):
                continue
            bookmaker_key = str(bookmaker["key"]).casefold()
            sportsbook = APPROVED_BOOKMAKERS.get(
                bookmaker_key, CONFIG.pinnacle_bookmaker_name
            )
            bookmaker_markets = bookmaker.get("markets")
            if not isinstance(bookmaker_markets, list):
                continue
            for market in bookmaker_markets:
                if not isinstance(market, dict):
                    continue
                market_key = market.get("key")
                if not _supported_market(market_key):
                    continue
                if not _is_main_line(market):
                    continue
                if _market_is_suspended(market):
                    continue
                if is_player_prop_market(market_key) and (
                    str(market_key).split(":", 1)[0].casefold()
                    not in allowed_prop_markets
                ):
                    continue
                player_prop = is_player_prop_market(market_key)
                for outcome in market.get("outcomes", []):
                    if not isinstance(outcome, dict):
                        continue
                    decimal_odds = american_to_decimal(outcome.get("price"))
                    if decimal_odds is None:
                        continue

                    player_id = None
                    player_name = None
                    if player_prop:
                        selection, player_id, player_name = _player_outcome(
                            market, outcome
                        )
                        if not selection:
                            continue
                        team = None
                        player_scope = _player_scope(player_id, player_name)
                        group_key = _market_identity(
                            market_key, outcome.get("point"), None, player_scope
                        )
                        instance_key = group_key
                    else:
                        selection = outcome.get("name")
                        if not isinstance(selection, str):
                            continue
                        team = _market_team(market, outcome)
                        player_scope = ""
                        group_key = _market_identity(
                            market_key, outcome.get("point"), team
                        )
                        instance_key = _market_identity(
                            market_key, outcome.get("point"), team, signed=True
                        )

                    point = outcome.get("point")
                    period = str(market.get("period") or "")
                    observed = event_markets.get(
                        (bookmaker_key, instance_key, period), set()
                    )
                    update = _parse_timestamp(market.get("last_update"))
                    latest_update = latest_market_updates.get(
                        (bookmaker_key, instance_key, period)
                    )
                    if latest_update is not None and update != latest_update:
                        continue
                    last_seen = _parse_timestamp(outcome.get("last_seen_at"))
                    if update is not None and last_seen is not None and last_seen < update:
                        continue

                    if player_prop:
                        if observed:
                            expected = (
                                ["Over", "Under"]
                                if observed == {"Over", "Under"}
                                else sorted(observed)
                            )
                        else:
                            expected = ["Over", "Under"]
                    elif market_key in {"h2h", "spreads"}:
                        side = outcome.get("side")
                        if side == "home":
                            selection = home
                        elif side == "away":
                            selection = away
                        elif side == "draw":
                            selection = "Draw"
                        elif selection.casefold() == home.casefold():
                            selection = home
                        elif selection.casefold() == away.casefold():
                            selection = away
                        elif selection.casefold() == "draw":
                            selection = "Draw"

                    if not player_prop and market_key == "h2h":
                        expected = [home, away]
                        if any(name.casefold() == "draw" for name in observed):
                            expected.append("Draw")
                    elif not player_prop and market_key == "spreads":
                        expected = [home, away]
                    elif not player_prop and market_key == "totals":
                        expected = ["Over", "Under"]
                        if selection.casefold() == "over":
                            selection = "Over"
                        elif selection.casefold() == "under":
                            selection = "Under"
                    elif not player_prop:
                        expected = sorted(observed)

                    # Only build the fields downstream consumers read. The VPS
                    # has 1 GB of RAM, so every unused key across ~130k quotes
                    # per scan costs real memory and swap.
                    quote = {
                        "event_id": str(event_id),
                        "sport": sport_label,
                        "matchup": f"{home} vs {away}",
                        "kickoff": event.get("commence_time") or "",
                        "is_live": event.get("live") is True,
                        "completed": event.get("completed") is True,
                        "market": _market_label(
                            market_key, point, team, player_name
                        ),
                        "line_type": market.get("line_type"),
                        "market_key": group_key,
                        "period": period,
                        "team": team,
                        "point": point,
                        "expected_outcomes": expected,
                        "selection": selection,
                        "sportsbook": sportsbook,
                        "bookmaker_key": bookmaker_key,
                        "odds_american": outcome.get("price"),
                        "odds_decimal": decimal_odds,
                        "price_format": "american",
                        "liquidity": outcome.get("liquidity"),
                        "liquidity_updated_at": outcome.get(
                            "liquidity_updated_at"
                        ),
                        "last_update": market.get("last_update"),
                        "last_change_at": outcome.get("last_change_at"),
                        "last_seen_at": outcome.get("last_seen_at"),
                        "book_updated_at": bookmaker.get("last_update"),
                        "is_player_prop": player_prop,
                        "player_id": player_id,
                        "player_name": player_name,
                    }
                    identity = (
                        str(event_id),
                        bookmaker_key,
                        instance_key,
                        period,
                        selection,
                    )
                    current = quotes_by_identity.get(identity)
                    current_update = (
                        _parse_timestamp(current.get("last_update"))
                        if current
                        else None
                    )
                    if (
                        current is None
                        or (update is not None and current_update is None)
                        or (
                            update is not None
                            and current_update is not None
                            and update > current_update
                        )
                        or (
                            update == current_update
                            and decimal_odds > current["odds_decimal"]
                        )
                    ):
                        quotes_by_identity[identity] = quote
    return list(quotes_by_identity.values())


def _fetch_sport(
    sport_label: str,
    mode: str,
    api_key: str,
    quota_state_file: str | None = None,
) -> dict[str, Any]:
    # Bound DNS resolution and TLS handshake, which requests' timeout does not cover.
    socket.setdefaulttimeout(CONFIG.api_timeout_seconds)
    result: dict[str, Any] = {"quotes": [], "errors": [], "warnings": [], "quota": {}}
    sport_key = SPORT_CONFIG[sport_label]
    session = _new_api_session()
    timeout_retries = 0
    try:
        params: dict[str, Any] = {
            "markets": sport_markets(sport_label),
            "bookmakers": ",".join(
                [*APPROVED_BOOKMAKERS, CONFIG.pinnacle_bookmaker_key]
            ),
            "max_age": CONFIG.api_max_age_seconds,
        }
        if CONFIG.include_depth:
            params["includeDepth"] = "true"
        if CONFIG.include_book_ids:
            params["includeBookIds"] = "true"
        if CONFIG.include_links:
            params["includeLinks"] = "true"
        request_args = {
            "params": params,
            "headers": {"X-API-Key": api_key.strip()},
            "timeout": CONFIG.api_timeout_seconds,
        }
        url = f"{API_BASE_URL}/sports/{sport_key}/odds"
        tls_retries = 0
        while True:
            try:
                response = session.get(url, **request_args)
                break
            except requests.exceptions.SSLError as error:
                if (
                    tls_retries == CONFIG.tls_retries
                    or _is_certificate_validation_error(error)
                ):
                    raise
                tls_retries += 1
                session.close()
                session = _new_api_session()
                time.sleep(0.5 * (2 ** (tls_retries - 1)))
            except requests.exceptions.Timeout:
                if timeout_retries == CONFIG.api_timeout_retries:
                    raise
                timeout_retries += 1
                session.close()
                session = _new_api_session()
                time.sleep(0.5 * (2 ** (timeout_retries - 1)))

        result["quota"] = parse_quota_headers(response.headers)
        if quota_state_file:
            record_quota(api_key, result["quota"], quota_state_file)

        if response.status_code == 401:
            result["errors"].append(
                f"{sport_label}: PropLine rejected the API key (HTTP 401)."
            )
            return result
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            detail = f" Retry after {retry_after} seconds." if retry_after else ""
            result["errors"].append(
                f"{sport_label}: PropLine rate or daily quota limit reached (HTTP 429)."
                f"{detail} No automatic retry was made."
            )
            return result
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            result["errors"].append(
                f"{sport_label}: PropLine returned invalid JSON: {error}."
            )
            return result
        finally:
            # Release the response (and its raw body, ~41 MB for NCAAF) before
            # parsing quotes so both copies are never alive at once.
            response.close()
            del response
        if not isinstance(payload, list) or not all(
            isinstance(event, dict) for event in payload
        ):
            result["errors"].append(
                f"{sport_label}: PropLine returned an invalid odds response."
            )
            return result

        matching_events = [
            event
            for event in payload
            if _event_is_in_mode(event, mode, datetime.now(EASTERN))
        ]
        # Drop the unfiltered response tree before parsing: holding the raw
        # payload and the parsed quotes at the same time is what pushed the
        # worker into swap on the 1 GB VPS.
        payload = None
        quotes: list[dict[str, Any]] = []
        for index in range(len(matching_events)):
            event = matching_events[index]
            matching_events[index] = None
            if event is not None:
                quotes.extend(parse_odds_payload([event], sport_label))
        result["quotes"] = quotes
        if not result["quotes"]:
            result["warnings"].append(
                f"{sport_label}: No {mode} odds were returned for the selected markets."
            )
    except requests.exceptions.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        detail = ""
        if error.response is not None:
            try:
                body = error.response.json()
                if isinstance(body, dict):
                    detail = body.get("message") or body.get("error") or ""
            except ValueError:
                detail = error.response.text.strip()
        result["errors"].append(
            f"{sport_label}: PropLine request failed (HTTP {status})."
            + (f" {detail}" if detail else "")
        )
    except requests.exceptions.SSLError as error:
        if _is_certificate_validation_error(error):
            result["errors"].append(
                f"{sport_label}: TLS certificate validation failed. "
                "Check your system certificate store, VPN, proxy, or HTTPS inspection "
                "settings. Certificate verification remains enabled."
            )
        else:
            result["errors"].append(
                f"{sport_label}: TLS handshake failed after "
                f"{CONFIG.tls_retries + 1} attempts; no HTTP response "
                f"was received ({error}). Check your network's IPv6 route, VPN, "
                "proxy, or firewall. "
                "Certificate verification remains enabled."
            )
    except requests.exceptions.Timeout as error:
        result["errors"].append(
            f"{sport_label}: PropLine request timed out after "
            f"{timeout_retries + 1} attempt(s) "
            f"(timeout={CONFIG.api_timeout_seconds}s): {error}."
        )
    except requests.exceptions.RequestException as error:
        result["errors"].append(f"{sport_label}: Could not reach PropLine: {error}.")
    finally:
        session.close()
    return result


def _merge_results(
    primary: dict[str, Any], secondary: dict[str, Any]
) -> dict[str, Any]:
    quotes = primary.get("quotes", []) + secondary.get("quotes", [])
    errors = primary.get("errors", []) + secondary.get("errors", [])
    warnings = primary.get("warnings", []) + secondary.get("warnings", [])
    quota: dict[str, str] = {}
    for source in (primary, secondary):
        for header, value in source.get("quota", {}).items():
            if header == "X-Daily-Remaining" and header in quota:
                try:
                    value = str(min(int(value), int(quota[header])))
                except ValueError:
                    pass
            quota[header] = value
    return {
        "quotes": quotes,
        "errors": errors,
        "warnings": warnings,
        "quota": quota,
        "request_count": primary.get("request_count", 0)
        + secondary.get("request_count", 0),
    }


def _result_needs_failover(
    result: dict[str, Any], failover_threshold: int
) -> bool:
    errors = result.get("errors", [])
    if any("429" in str(error) for error in errors):
        return True
    remaining = result.get("quota", {}).get("X-Daily-Remaining")
    if remaining is None:
        return False
    try:
        return int(remaining) < failover_threshold
    except (TypeError, ValueError):
        return False


def _run_api_with_key(
    sports: list[str],
    mode: str,
    api_key: str,
    quota_state_file: str | None,
) -> dict[str, Any]:
    max_workers = max(1, min(CONFIG.api_max_workers, len(sports)))
    results: list[dict[str, Any]] = []
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {
        executor.submit(_fetch_sport, sport, mode, api_key, quota_state_file): sport
        for sport in sports
    }
    # Budget each sport separately: one stalled request must not cancel the
    # sports queued behind it. The socket default timeout bounds the request
    # itself, so this only catches a thread that still will not finish.
    per_sport_budget = CONFIG.api_timeout_seconds * 2 + 10
    try:
        for future, sport in futures.items():
            try:
                results.append(future.result(timeout=per_sport_budget))
            except FuturesTimeoutError:
                future.cancel()
                results.append(
                    {
                        "quotes": [],
                        "errors": [
                            f"{sport}: no response within {per_sport_budget}s."
                        ],
                        "warnings": [],
                        "quota": {},
                    }
                )
            except Exception as error:
                results.append(
                    {
                        "quotes": [],
                        "errors": [f"{sport}: {error}"],
                        "warnings": [],
                        "quota": {},
                    }
                )
    finally:
        executor.shutdown(wait=False)

    quotes = [quote for result in results for quote in result["quotes"]]
    errors = [error for result in results for error in result["errors"]]
    warnings = [warning for result in results for warning in result["warnings"]]
    quota: dict[str, str] = {}
    for result in results:
        for header, value in result["quota"].items():
            if header == "X-Daily-Remaining" and header in quota:
                try:
                    value = str(min(int(value), int(quota[header])))
                except ValueError:
                    pass
            quota[header] = value
    return {
        "quotes": quotes,
        "errors": errors,
        "warnings": warnings,
        "request_count": len(sports),
        "quota": quota,
    }


def run_api(
    sports: list[str],
    mode: str = "upcoming",
    api_key: str = "",
    api_keys: list[str] | None = None,
    now: datetime | None = None,
    quota_state_file: str | None = None,
    failover_threshold: int = CONFIG.quota_failover_threshold,
) -> dict[str, Any]:
    if mode not in {"upcoming", "live"}:
        raise ValueError("Mode must be 'upcoming' or 'live'.")
    invalid_sports = [sport for sport in sports if sport not in SPORT_CONFIG]
    if invalid_sports:
        raise ValueError(f"Unsupported sports: {', '.join(invalid_sports)}")
    if not sports:
        return {
            "quotes": [],
            "errors": ["Select at least one sport to scan."],
            "warnings": [],
            "request_count": 0,
            "quota": {},
        }

    explicit_key = api_key.strip() if isinstance(api_key, str) else ""
    if api_keys:
        keys = list(
            dict.fromkeys(
                key.strip() for key in api_keys if isinstance(key, str) and key.strip()
            )
        )
    elif explicit_key:
        keys = [explicit_key]
    else:
        keys = select_api_keys(now, quota_state_file, failover_threshold)

    if not keys:
        return {
            "quotes": [],
            "errors": ["Enter your PropLine API key to scan odds."],
            "warnings": [],
            "request_count": 0,
            "quota": {},
        }

    if quota_state_file is None and not explicit_key and len(keys) > 0:
        quota_state_file = CONFIG.quota_state_file

    primary_key = keys[0]
    secondary_key = keys[1] if len(keys) > 1 else None
    result = _run_api_with_key(
        sports, mode, primary_key, quota_state_file
    )

    if (
        secondary_key
        and _result_needs_failover(result, failover_threshold)
    ):
        secondary_result = _run_api_with_key(
            sports, mode, secondary_key, quota_state_file
        )
        merged = _merge_results(result, secondary_result)
        merged["failover_used"] = True
        merged["warnings"] = [
            *merged["warnings"],
            "Primary PropLine key was low or rate-limited; failed over to the secondary key for this scan.",
        ]
        return merged

    result["failover_used"] = False
    return result


def fetch_sport_events(sport_label: str, api_key: str) -> list[dict[str, Any]]:
    # Free-tier event list used for active-sport detection. Returns [] on any error.
    socket.setdefaulttimeout(CONFIG.api_timeout_seconds)
    sport_key = SPORT_CONFIG.get(sport_label)
    if not sport_key or not api_key or not api_key.strip():
        return []
    session = _new_api_session()
    try:
        response = session.get(
            f"{API_BASE_URL}/sports/{sport_key}/events",
            headers={"X-API-Key": api_key.strip()},
            timeout=CONFIG.api_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except (requests.RequestException, ValueError):
        return []
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Free-tier enrichments: freshness (no auth) and injury reports
# ---------------------------------------------------------------------------


def fetch_freshness() -> dict[str, Any]:
    """Fetch per-book staleness from /v1/freshness (no API key required)."""
    socket.setdefaulttimeout(CONFIG.api_timeout_seconds)
    session = _new_api_session()
    try:
        response = session.get(f"{API_BASE_URL}/freshness", timeout=CONFIG.api_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except (requests.RequestException, ValueError):
        return {}
    finally:
        session.close()


def stale_book_keys(freshness: dict[str, Any], max_age_seconds: int | None = None) -> set[str]:
    """Return bookmaker keys whose game-line or prop board is stale."""
    threshold = max_age_seconds
    if threshold is None:
        try:
            threshold = int(freshness.get("stale_threshold_seconds", 0) or 0)
        except (TypeError, ValueError):
            threshold = 0
    if not threshold:
        threshold = 300
    stale: set[str] = set()
    for book in freshness.get("bookmakers", []) or []:
        if not isinstance(book, dict):
            continue
        key = str(book.get("key") or "").strip().lower()
        if not key:
            continue
        if book.get("is_stale"):
            stale.add(key)
            continue
        for class_name in ("game_lines", "props"):
            info = (book.get("market_classes") or {}).get(class_name, {})
            if not isinstance(info, dict):
                continue
            if info.get("is_stale"):
                stale.add(key)
                break
            staleness = info.get("staleness_seconds")
            if staleness is not None and int(staleness) > threshold:
                stale.add(key)
                break
        # worst_staleness_seconds is the age of the oldest non-suspended market on
        # the active board and is expected to be large; it is not a staleness
        # signal. Only is_stale / per-class staleness above the threshold count.
    return stale


def fetch_injuries(sport_label: str, api_key: str) -> list[dict[str, Any]]:
    """Fetch the current injury report for a sport (free tier, any key)."""
    socket.setdefaulttimeout(CONFIG.api_timeout_seconds)
    sport_key = SPORT_CONFIG.get(sport_label)
    if not sport_key or not api_key or not api_key.strip():
        return []
    session = _new_api_session()
    url = f"{API_BASE_URL}/sports/{sport_key}/injuries"
    try:
        response = session.get(url, headers={"X-API-Key": api_key.strip()}, timeout=CONFIG.api_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        players = payload.get("players") if isinstance(payload, dict) else None
        return players if isinstance(players, list) else []
    except (requests.RequestException, ValueError):
        return []
    finally:
        session.close()


def injured_player_names(injuries: list[dict[str, Any]], statuses: set[str] | None = None) -> set[str]:
    """Return normalized player names whose status is in ``statuses``."""
    if statuses is None:
        statuses = {"out", "doubtful", "questionable", "day_to_day"}
    names: set[str] = set()
    for player in injuries or []:
        if not isinstance(player, dict):
            continue
        code = str(player.get("status_code") or "").strip().lower()
        if code not in statuses:
            continue
        name = " ".join(str(player.get("player_name") or "").split()).casefold()
        if name:
            names.add(name)
    return names
