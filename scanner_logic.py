import re
import unicodedata
from collections import defaultdict
from math import ceil, isfinite
from statistics import NormalDist, median
from datetime import datetime, timezone
from typing import Any

from scanner_config import CONFIG, DEVIG_METHODS

PLAYER_PROP_PREFIX = "player_"
_MILESTONE_SELECTION = re.compile(r"^\s*\d+\s*\+\s*$")


def _expected_profit_per_dollar(
    decimal_odds: float, fair_probability: float
) -> float:
    if (
        not isfinite(decimal_odds)
        or decimal_odds <= 1
        or not isfinite(fair_probability)
        or not 0 <= fair_probability <= 1
    ):
        raise ValueError("Decimal odds must exceed 1 and probability must be between 0 and 1.")
    return (
        fair_probability * (decimal_odds - 1)
        - (1 - fair_probability)
    )


def _validate_book_fees(config=CONFIG) -> None:
    for _, fee in config.book_fee_pct:
        if not isfinite(fee) or not 0 <= fee < 1:
            raise ValueError("Book fee must be a fraction from 0 up to, not including, 1.")


def _book_fee_fraction(bookmaker: str, config=CONFIG) -> float:
    for name, fee in config.book_fee_pct:
        if name == bookmaker:
            return float(fee)
    return 0.0


def net_decimal_odds(decimal_odds: float, fee: float) -> float:
    """Decimal odds after a fee on winnings: 1 + (d - 1) * (1 - fee)."""
    return 1 + (decimal_odds - 1) * (1 - fee)


def expected_value_pct(decimal_odds: float, fair_probability: float) -> float:
    return _expected_profit_per_dollar(decimal_odds, fair_probability) * 100


def expected_value_dollars(
    decimal_odds: float, fair_probability: float, stake: float
) -> float:
    if not isfinite(stake) or stake < 0:
        raise ValueError("Stake must be a nonnegative finite amount.")
    return _expected_profit_per_dollar(decimal_odds, fair_probability) * stake


def expected_value_with_push_pct(
    decimal_odds: float, win_probability: float, lose_probability: float
) -> float:
    if (
        not isfinite(decimal_odds)
        or decimal_odds <= 1
        or not isfinite(win_probability)
        or not isfinite(lose_probability)
        or win_probability < 0
        or lose_probability < 0
        or win_probability + lose_probability > 1 + 1e-9
    ):
        raise ValueError("Odds and outcome probabilities are invalid.")
    return (
        win_probability * (decimal_odds - 1) - lose_probability
    ) * 100


def devig_two_way(
    decimal_a: float, decimal_b: float, method: str = CONFIG.devig_method
) -> tuple[float, float]:
    if (
        not isfinite(decimal_a)
        or not isfinite(decimal_b)
        or decimal_a <= 1
        or decimal_b <= 1
    ):
        raise ValueError("Both decimal prices must be finite and greater than 1.")
    if method not in DEVIG_METHODS:
        raise ValueError(f"Unsupported de-vig method: {method}.")

    implied_a = 1 / decimal_a
    implied_b = 1 / decimal_b
    overround = implied_a + implied_b
    if overround < 1 - 1e-12:
        raise ValueError("A two-way Pinnacle reference must have an overround of at least 1.")

    if method == "multiplicative":
        return implied_a / overround, implied_b / overround

    if method == "power":
        if abs(overround - 1) <= 1e-12:
            return implied_a, implied_b
        low, high = 1.0, 10.0
        if implied_a**high + implied_b**high > 1:
            raise ValueError("Power de-vig root is outside the configured range.")
        for _ in range(100):
            exponent = (low + high) / 2
            if implied_a**exponent + implied_b**exponent > 1:
                low = exponent
            else:
                high = exponent
        exponent = (low + high) / 2
        return implied_a**exponent, implied_b**exponent

    if abs(overround - 1) <= 1e-12:
        return implied_a, implied_b
    if abs(implied_a - implied_b) <= 1e-12:
        return 0.5, 0.5

    def shin_probability(implied: float, z: float) -> float:
        return (
            (z * z + 4 * (1 - z) * implied * implied / overround) ** 0.5 - z
        ) / (2 * (1 - z))

    low, high = 0.0, 1 - 1e-12
    for _ in range(100):
        z = (low + high) / 2
        fair_a = shin_probability(implied_a, z)
        fair_b = shin_probability(implied_b, z)
        if fair_a + fair_b > 1:
            low = z
        else:
            high = z
    z = (low + high) / 2
    return shin_probability(implied_a, z), shin_probability(implied_b, z)


def filter_quotes(
    quotes: list[dict[str, Any]], selected_sports: list[str], selected_books: list[str]
) -> list[dict[str, Any]]:
    return [
        quote
        for quote in quotes
        if quote["sport"] in selected_sports and quote["sportsbook"] in selected_books
    ]


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _quote_time(quote: dict[str, Any]) -> datetime | None:
    for field in ("last_seen_at", "last_update", "book_updated_at", "last_change_at"):
        parsed = _parse_utc(quote.get(field))
        if parsed is not None:
            return parsed
    return None


def _quote_time_text(quote: dict[str, Any]) -> str | None:
    timestamp = _quote_time(quote)
    return timestamp.isoformat() if timestamp else None


def _base_market(quote: dict[str, Any]) -> str:
    return str(quote.get("market_key") or "").split(":", 1)[0].casefold()


def _numeric_point(quote: dict[str, Any]) -> float | None:
    try:
        point = float(quote.get("point"))
    except (TypeError, ValueError):
        return None
    return point if isfinite(point) else None


def _reference_key(quote: dict[str, Any]) -> tuple[Any, ...] | None:
    market = _base_market(quote)
    point = _numeric_point(quote)
    if market in {"spreads", "totals"} and point is None:
        return None
    if market == "spreads":
        line = round(abs(point), 6)
    elif market == "totals":
        line = round(point, 6)
    else:
        line = None
    team_scope = (
        str(quote.get("team") or "").strip().casefold()
        if market == "totals"
        else ""
    )
    return (
        str(quote.get("sport") or ""),
        str(quote.get("event_id") or ""),
        market,
        str(quote.get("period") or ""),
        team_scope,
        line,
    )


def _selection_coordinate(market: str, selection: str, point: float) -> float | None:
    if market == "spreads":
        return point
    if market == "totals":
        normalized_selection = selection.strip().casefold()
        if normalized_selection == "over":
            return -point
        if normalized_selection == "under":
            return point
    return None


def _is_whole_line(point: float) -> bool:
    return abs(point - round(point)) <= 1e-6


def _normal_conditional_probability(
    mean: float, standard_deviation: float, whole_line: bool
) -> float:
    distribution = NormalDist(mean, standard_deviation)
    boundary = 0.5 if whole_line else 0.0
    win_probability = 1 - distribution.cdf(boundary)
    lose_probability = distribution.cdf(-boundary)
    resolved_probability = win_probability + lose_probability
    return win_probability / resolved_probability if resolved_probability else 0.5


def _normal_mean_for_probability(
    probability: float, standard_deviation: float, whole_line: bool
) -> float:
    target = min(max(probability, 1e-9), 1 - 1e-9)
    extent = max(20 * standard_deviation, 2.0 if whole_line else 0.0)
    low, high = -extent, extent
    for _ in range(100):
        mean = (low + high) / 2
        if _normal_conditional_probability(
            mean, standard_deviation, whole_line
        ) < target:
            low = mean
        else:
            high = mean
    return (low + high) / 2


def _normal_outcome_probabilities(
    probability: float, standard_deviation: float, whole_line: bool
) -> tuple[float, float, float]:
    if not whole_line:
        return probability, 1 - probability, 0.0
    mean = _normal_mean_for_probability(probability, standard_deviation, True)
    distribution = NormalDist(mean, standard_deviation)
    win_probability = 1 - distribution.cdf(0.5)
    lose_probability = distribution.cdf(-0.5)
    push_probability = max(0.0, 1 - win_probability - lose_probability)
    total = win_probability + lose_probability + push_probability
    return (
        win_probability / total,
        lose_probability / total,
        push_probability / total,
    )


def _standard_deviation_for_sport(
    sport: str, market: str, config=CONFIG
) -> float | None:
    standard_deviation = next(
        (
            value
            for (name, name_market), value in config.line_adjusted_stddev_by_sport
            if name.casefold() == sport.casefold()
            and name_market == market.casefold()
        ),
        None,
    )
    if (
        standard_deviation is None
        or not isfinite(standard_deviation)
        or standard_deviation <= 0
    ):
        return None
    return standard_deviation


def estimate_line_adjusted_probability(
    sport: str,
    market: str,
    selection: str,
    target_point: float,
    references: list[dict[str, Any]],
    config=CONFIG,
) -> dict[str, Any] | None:
    if market not in {"spreads", "totals"} or not isfinite(target_point):
        return None

    standard_deviation = _standard_deviation_for_sport(sport, market, config)
    usable = []
    for reference in references:
        try:
            point = float(reference["point"])
            probability = float(reference["fair_probability"])
            overround = float(reference.get("overround", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        coordinate = _selection_coordinate(market, selection, point)
        if (
            coordinate is not None
            and isfinite(point)
            and isfinite(probability)
            and 0 < probability < 1
            and isfinite(overround)
        ):
            usable.append(
                {
                    **reference,
                    "point": point,
                    "coordinate": coordinate,
                    "fair_probability": probability,
                    "overround": overround,
                }
            )
    if not usable:
        return None

    nearest_distance = min(abs(item["point"] - target_point) for item in usable)
    if nearest_distance > config.max_line_diff + 1e-9:
        return None
    target_coordinate = _selection_coordinate(market, selection, target_point)
    if target_coordinate is None:
        return None

    exact = next(
        (item for item in usable if abs(item["point"] - target_point) <= 1e-6),
        None,
    )
    if exact is not None:
        probability = exact["fair_probability"]
        win, lose, push = _normal_outcome_probabilities(
            probability,
            standard_deviation,
            _is_whole_line(target_point),
        ) if _is_whole_line(target_point) and standard_deviation else (
            probability,
            1 - probability,
            0.0,
        )
        return {
            "method": "exact",
            "fair_probability": probability,
            "win_probability": win,
            "lose_probability": lose,
            "push_probability": push,
            "source_references": [exact],
        }

    if _is_whole_line(target_point):
        by_coordinate = {round(item["coordinate"], 6): item for item in usable}
        strict = by_coordinate.get(round(target_coordinate - 0.5, 6))
        loose = by_coordinate.get(round(target_coordinate + 0.5, 6))
        if (
            strict is not None
            and loose is not None
            and loose["fair_probability"] >= strict["fair_probability"]
        ):
            win = strict["fair_probability"]
            lose = 1 - loose["fair_probability"]
            push = loose["fair_probability"] - strict["fair_probability"]
            resolved = win + lose
            return {
                "method": "bracket_exact",
                "fair_probability": win / resolved,
                "win_probability": win,
                "lose_probability": lose,
                "push_probability": push,
                "source_references": [strict, loose],
            }

    ordered = sorted(usable, key=lambda item: item["coordinate"])
    bracket = None
    for left, right in zip(ordered, ordered[1:]):
        if (
            left["coordinate"] < target_coordinate < right["coordinate"]
            and abs(left["point"] - target_point) <= config.max_line_diff + 1e-9
            and abs(right["point"] - target_point) <= config.max_line_diff + 1e-9
        ):
            bracket = (left, right)
            break

    if bracket is not None:
        left, right = bracket
        fraction = (
            (target_coordinate - left["coordinate"])
            / (right["coordinate"] - left["coordinate"])
        )
        probability = left["fair_probability"] + fraction * (
            right["fair_probability"] - left["fair_probability"]
        )
        if _is_whole_line(target_point):
            if standard_deviation is None or standard_deviation <= 0:
                return None
            win, lose, push = _normal_outcome_probabilities(
                probability, standard_deviation, True
            )
        else:
            win, lose, push = probability, 1 - probability, 0.0
        return {
            "method": "interpolation",
            "fair_probability": probability,
            "win_probability": win,
            "lose_probability": lose,
            "push_probability": push,
            "source_references": [left, right],
        }

    if standard_deviation is None or standard_deviation <= 0:
        return None
    mainline = min(
        usable,
        key=lambda item: (
            abs(item["fair_probability"] - 0.5),
            item["overround"],
        ),
    )
    if abs(mainline["point"] - target_point) > config.max_line_diff + 1e-9:
        return None

    anchor_whole = _is_whole_line(mainline["point"])
    mean_at_anchor = _normal_mean_for_probability(
        mainline["fair_probability"], standard_deviation, anchor_whole
    )
    anchor_coordinate = _selection_coordinate(
        market, selection, mainline["point"]
    )
    target_whole = _is_whole_line(target_point)
    target_mean = mean_at_anchor + target_coordinate - anchor_coordinate
    probability = _normal_conditional_probability(
        target_mean, standard_deviation, target_whole
    )

    if sport.casefold() in {"nfl", "ncaaf"} and market == "spreads":
        direction = 1 if target_coordinate > anchor_coordinate else -1
        key_adjustment = sum(
            direction * boost
            for key_number, boost in config.football_key_number_probability_boosts
            for signed_key in (-key_number, key_number)
            if min(mainline["point"], target_point)
            < signed_key
            < max(mainline["point"], target_point)
        )
        probability = min(max(probability + key_adjustment, 1e-9), 1 - 1e-9)

    if target_whole:
        win, lose, push = _normal_outcome_probabilities(
            probability, standard_deviation, True
        )
    else:
        win, lose, push = probability, 1 - probability, 0.0
    return {
        "method": "distribution",
        "fair_probability": probability,
        "win_probability": win,
        "lose_probability": lose,
        "push_probability": push,
        "source_references": [mainline],
    }


def _valid_two_way_market(
    market: str, quotes_by_selection: dict[str, dict[str, Any]], expected: set[str]
) -> bool:
    if len(expected) != 2 or set(quotes_by_selection) != expected:
        return False
    if market == "spreads":
        points = [_numeric_point(quotes_by_selection[selection]) for selection in expected]
        return (
            all(point is not None and point != 0 for point in points)
            and abs(abs(points[0]) - abs(points[1])) <= 1e-6
            and points[0] * points[1] < 0
            and abs(points[0] + points[1]) <= 1e-6
        )
    if market == "totals":
        points = [_numeric_point(quotes_by_selection[selection]) for selection in expected]
        return (
            all(point is not None for point in points)
            and abs(points[0] - points[1]) <= 1e-6
        )
    return market == "h2h"


def _has_conflicting_prices(candidates: list[dict[str, Any]]) -> bool:
    """True when quotes sharing a timestamp disagree on price."""
    prices_by_time: dict[str | None, set[float]] = defaultdict(set)
    for candidate in candidates:
        try:
            prices_by_time[_quote_time_text(candidate)].add(float(candidate["odds_decimal"]))
        except (KeyError, TypeError, ValueError):
            return True
    return any(len(prices) > 1 for prices in prices_by_time.values())


def _quote_is_stale(
    quote: dict[str, Any], now: datetime, max_age_seconds: int
) -> bool:
    timestamp = _quote_time(quote)
    return bool(
        timestamp is not None
        and (now - timestamp).total_seconds() > max_age_seconds
    )


def _american_from_decimal(decimal_odds: float) -> int:
    if decimal_odds >= 2:
        return int(round((decimal_odds - 1) * 100))
    return int(round(-100 / (decimal_odds - 1)))


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def _american_price_to_decimal(value: Any) -> float | None:
    """Convert a published depth price (American) to decimal odds."""
    try:
        american = int(value)
    except (TypeError, ValueError):
        return None
    if american > 0:
        return 1.0 + american / 100.0
    if american < 0:
        return 1.0 - 100.0 / american
    return None


def executable_odds_for_stake(
    quote: dict[str, Any], target_stake: float
) -> dict[str, Any]:
    """Average odds fillable for ``target_stake`` at this quote.

    PropLine reports ``liquidity`` as the size at the quoted price and, for
    exchange books only, ``depth`` as further price/size levels beyond it. A
    stake larger than the top level fills part of the bet at worse odds, so the
    quoted price overstates the edge. Books without size information are
    valued entirely at the quoted price and flagged as unmodelled.
    """
    stake = _finite_positive(target_stake)
    quoted = _finite_positive(quote.get("odds_decimal"))
    if stake is None or quoted is None:
        return {
            "executable_decimal": quoted or 0.0,
            "levels_used": 0,
            "slippage_pct": 0.0,
            "size_unknown": True,
        }

    depth = quote.get("depth") or []
    remaining = stake
    total_return = 0.0
    levels_used = 0
    size_unknown = False
    top_size = _finite_positive(quote.get("liquidity"))
    if top_size is None:
        size_unknown = True
        total_return = stake * quoted
    else:
        top_fill = min(remaining, top_size)
        total_return += top_fill * quoted
        remaining -= top_fill
        for level in depth:
            if not isinstance(level, dict) or remaining <= 1e-9:
                continue
            price = _american_price_to_decimal(level.get("price"))
            level_size = _finite_positive(level.get("size"))
            if price is None or price <= 1 or level_size is None:
                continue
            fill = min(remaining, level_size)
            total_return += fill * price
            remaining -= fill
            levels_used += 1
        if remaining > 1e-9:
            # Past the last published level the price is unknown. Value the
            # remainder at the worst published level, never at the top price.
            worst = quoted
            for level in depth:
                if not isinstance(level, dict):
                    continue
                price = _american_price_to_decimal(level.get("price"))
                if price is not None and 1 < price < worst:
                    worst = price
            total_return += remaining * worst
            size_unknown = True

    executable = total_return / stake
    return {
        "executable_decimal": executable,
        "levels_used": levels_used,
        "slippage_pct": (1.0 - executable / quoted) * 100.0,
        "size_unknown": size_unknown,
    }


def _is_pinnacle_quote(quote: dict[str, Any], config=CONFIG) -> bool:
    bookmaker_key = str(quote.get("bookmaker_key") or "").casefold()
    sportsbook = str(quote.get("sportsbook") or "").casefold()
    return (
        bookmaker_key == config.pinnacle_bookmaker_key.casefold()
        or sportsbook == config.pinnacle_bookmaker_name.casefold()
        or sportsbook == "pinnacle"
        or "pinnacle" in sportsbook
    )


def is_player_prop_market_key(market_key: Any) -> bool:
    return str(market_key or "").split(":", 1)[0].casefold().startswith(
        PLAYER_PROP_PREFIX
    )


def is_player_prop_quote(quote: dict[str, Any]) -> bool:
    if quote.get("is_player_prop") is True:
        return True
    return is_player_prop_market_key(
        quote.get("market_key") or quote.get("market")
    )


def _is_milestone_quote(quote: dict[str, Any]) -> bool:
    """One-way milestone (e.g. ``30+``) outcomes cannot form a two-way arb."""
    selection = str(quote.get("selection") or "").strip()
    market_key = str(quote.get("market_key") or quote.get("market") or "").casefold()
    if "milestone" in market_key:
        return True
    return bool(_MILESTONE_SELECTION.match(selection))


_PLAYER_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def _normalize_player_name(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    parts = text.split()
    while parts and parts[-1].casefold().rstrip(".") in {
        suffix.rstrip(".") for suffix in _PLAYER_NAME_SUFFIXES
    }:
        parts.pop()
    return " ".join(parts).casefold()


def _player_identity(quote: dict[str, Any]) -> str:
    player_id = quote.get("player_id")
    if player_id is not None and str(player_id).strip():
        return f"id:{str(player_id).strip()}"
    player_name = _normalize_player_name(quote.get("player_name"))
    return f"name:{player_name}" if player_name else ""


def _event_started(quote: dict[str, Any], now: datetime) -> tuple[bool, bool]:
    if quote.get("completed") is True or quote.get("is_live") is True:
        return True, False
    kickoff = _parse_utc(quote.get("kickoff"))
    if kickoff is None:
        return False, True
    return kickoff <= now, False


def _drop_reason_for_missing_reference(
    ref_key: tuple[Any, ...] | None,
    reference_status: dict[tuple[Any, ...], dict[str, Any]],
) -> str:
    if ref_key is None:
        return "line_mismatch"
    status = reference_status.get(ref_key)
    if status:
        return status["status"]

    sport, event_id, market, period, team_scope, _line = ref_key
    same_event_market = [
        key
        for key in reference_status
        if key[0] == sport and key[1] == event_id and key[2] == market
    ]
    same_scope_period = [
        key
        for key in same_event_market
        if key[3] == period and key[4] == team_scope
    ]
    if same_scope_period:
        return "line_mismatch"
    if any(key[3] != period for key in same_event_market):
        return "period_mismatch"
    if market == "totals" and any(
        key[3] == period and key[4] != team_scope for key in same_event_market
    ):
        return "team_scope_mismatch"
    return "no_pinnacle_reference"


def deduplicate_alt_line_ladders(
    candidate_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    best_by_selection: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in candidate_rows:
        market = _base_market(row)
        key = (
            row.get("sport", ""),
            row.get("event_id", ""),
            market,
            row.get("period", ""),
            str(row.get("team") or "").strip().casefold()
            if market == "totals"
            else "",
            row.get("selection", ""),
        )
        current = best_by_selection.get(key)
        if current is None or row["ev_pct"] > current["ev_pct"]:
            best_by_selection[key] = row
    opportunities = sorted(
        best_by_selection.values(), key=lambda row: row["ev_pct"], reverse=True
    )
    return opportunities, len(candidate_rows) - len(best_by_selection)


def scan_pinnacle_ev(
    quotes: list[dict[str, Any]],
    config=CONFIG,
    now: datetime | None = None,
) -> dict[str, Any]:
    _validate_book_fees(config)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)

    diagnostics = {
        "rows_fetched": len(quotes),
        "bettable_quotes": 0,
        "dropped": {
            "no_pinnacle_reference": 0,
            "one_sided_pinnacle_market": 0,
            "line_mismatch": 0,
            "period_mismatch": 0,
            "team_scope_mismatch": 0,
            "stale_quote": 0,
            "overround_too_high": 0,
            "started": 0,
            "missing_start_time": 0,
            "invalid_pinnacle_market": 0,
            "below_min_ev": 0,
            "below_min_liquidity": 0,
            "alt_line_duplicate": 0,
            "ambiguous_pinnacle_quote": 0,
            "player_prop_excluded": 0,
        },
        "rows_kept": 0,
        "liquidity_slippage_applied": 0,
        "liquidity_size_unknown": 0,
        "untimestamped_quotes_used": 0,
        "ev_pct": {"min": None, "median": None, "max": None},
    }

    # Player props are arbitrage-only. They must never receive a Pinnacle
    # +EV/CLV evaluation, snapshot, or row.
    excluded_player_props = [
        quote for quote in quotes if is_player_prop_quote(quote)
    ]
    diagnostics["dropped"]["player_prop_excluded"] = len(excluded_player_props)
    ev_quotes = [
        quote for quote in quotes if not is_player_prop_quote(quote)
    ]

    pinnacle_quotes = [
        quote for quote in ev_quotes if _is_pinnacle_quote(quote, config)
    ]
    bettable_quotes = [
        quote
        for quote in ev_quotes
        if not _is_pinnacle_quote(quote, config)
        and not (
            str(quote.get("sport") or "").casefold() == "ncaab"
            and str(quote.get("line_type") or "").casefold() == "alternate"
        )
    ]
    diagnostics["bettable_quotes"] = len(bettable_quotes)

    grouped_pinnacle: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    reference_status: dict[tuple[Any, ...], dict[str, Any]] = {}
    for quote in pinnacle_quotes:
        key = _reference_key(quote)
        if key is not None:
            grouped_pinnacle[key].append(quote)

    valid_references: dict[tuple[Any, ...], dict[str, Any]] = {}
    pinnacle_snapshots = []
    for key, market_quotes in grouped_pinnacle.items():
        market = key[2]
        expected_values = [
            set(quote.get("expected_outcomes") or [])
            for quote in market_quotes
            if quote.get("expected_outcomes")
        ]
        expected = expected_values[0] if expected_values else {
            quote.get("selection") for quote in market_quotes
        }
        by_selection = {}
        candidates_by_selection: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for quote in market_quotes:
            selection = quote.get("selection")
            if isinstance(selection, str):
                by_selection[selection] = quote
                candidates_by_selection[selection].append(quote)
        ambiguous_selections = {
            selection
            for selection, candidates in candidates_by_selection.items()
            if _has_conflicting_prices(candidates)
        }
        for selection in ambiguous_selections:
            by_selection.pop(selection, None)
        if len(expected) != 2:
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        if len(expected_values) > 1 and any(
            value != expected for value in expected_values[1:]
        ):
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        if len(by_selection) > 2:
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        if len(by_selection) < 2 or set(by_selection) != expected:
            status = (
                "ambiguous_pinnacle_quote"
                if ambiguous_selections
                else "one_sided_pinnacle_market"
            )
            reference_status[key] = {"status": status}
            continue
        if not _valid_two_way_market(market, by_selection, expected):
            reference_status[key] = {"status": "line_mismatch"}
            continue

        selection_names = list(expected)
        quote_a = by_selection[selection_names[0]]
        quote_b = by_selection[selection_names[1]]
        try:
            decimal_a = float(quote_a["odds_decimal"])
            decimal_b = float(quote_b["odds_decimal"])
            overround = 1 / decimal_a + 1 / decimal_b
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        if (
            not isfinite(decimal_a)
            or not isfinite(decimal_b)
            or decimal_a <= 1
            or decimal_b <= 1
            or overround < 1 - 1e-12
        ):
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        if overround > config.max_pinnacle_overround:
            reference_status[key] = {"status": "overround_too_high"}
            continue
        try:
            fair_a, fair_b = devig_two_way(
                decimal_a, decimal_b, method=config.devig_method
            )
        except ValueError:
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        fair_by_selection = {
            selection_names[0]: fair_a,
            selection_names[1]: fair_b,
        }
        reference = {
            "quotes": by_selection,
            "fair_by_selection": fair_by_selection,
            "overround": overround,
            "selections": selection_names,
        }
        valid_references[key] = reference
        reference_status[key] = {"status": "valid", **reference}

        is_stale = any(
            _quote_is_stale(quote, now_utc, config.max_quote_age_seconds)
            for quote in (quote_a, quote_b)
        )
        started, missing_start = _event_started(quote_a, now_utc)
        if not is_stale and not started and not missing_start:
            for selection in selection_names:
                pinnacle_quote = by_selection[selection]
                pinnacle_snapshots.append(
                    {
                        "sport": pinnacle_quote.get("sport", ""),
                        "event_id": pinnacle_quote.get("event_id", ""),
                        "market_key": _base_market(pinnacle_quote),
                        "period": pinnacle_quote.get("period", ""),
                        "team": pinnacle_quote.get("team") or "",
                        "selection": selection,
                        "point": pinnacle_quote.get("point"),
                        "kickoff": pinnacle_quote.get("kickoff", ""),
                        "fair_probability": fair_by_selection[selection],
                        "pinnacle_american": pinnacle_quote.get("odds_american")
                        or _american_from_decimal(float(pinnacle_quote["odds_decimal"])),
                        "pinnacle_decimal": float(pinnacle_quote["odds_decimal"]),
                        "overround": overround,
                        "quote_time": _quote_time_text(pinnacle_quote),
                    }
                )

    references_by_scope: dict[
        tuple[Any, ...], list[tuple[tuple[Any, ...], dict[str, Any]]]
    ] = defaultdict(list)
    for reference_key, reference in valid_references.items():
        references_by_scope[reference_key[:5]].append((reference_key, reference))

    candidate_rows = []
    for quote in bettable_quotes:
        started, missing_start = _event_started(quote, now_utc)
        if started:
            diagnostics["dropped"]["started"] += 1
            continue
        if missing_start:
            diagnostics["dropped"]["missing_start_time"] += 1
            continue
        liquidity = quote.get("liquidity")
        min_liquidity = config.min_liquidity_dollars
        if str(quote.get("sport") or "").casefold() == "ncaab":
            min_liquidity = max(
                min_liquidity,
                getattr(config, "ncaab_min_liquidity_dollars", min_liquidity),
            )
        if liquidity is not None and not isinstance(liquidity, bool):
            try:
                liquidity_dollars = float(liquidity)
            except (TypeError, ValueError):
                liquidity_dollars = None
            if (
                liquidity_dollars is not None
                and isfinite(liquidity_dollars)
                and liquidity_dollars < min_liquidity
            ):
                diagnostics["dropped"]["below_min_liquidity"] += 1
                continue

        key = _reference_key(quote)
        reference = valid_references.get(key)
        line_pricing = None
        if (
            reference is None
            and config.enable_line_adjusted
            and key is not None
            and key not in reference_status
            and key[2] in {"spreads", "totals"}
        ):
            selection = quote.get("selection")
            candidate_expected = set(quote.get("expected_outcomes") or [])
            line_references = []
            if isinstance(selection, str):
                for _, candidate_reference in references_by_scope.get(
                    key[:5], []
                ):
                    if candidate_expected and candidate_expected != set(
                        candidate_reference["selections"]
                    ):
                        continue
                    side_quote = candidate_reference["quotes"].get(selection)
                    if side_quote is None:
                        continue
                    pair = list(candidate_reference["quotes"].values())
                    if any(
                        _quote_is_stale(
                            item, now_utc, config.max_quote_age_seconds
                        )
                        for item in pair
                    ):
                        continue
                    side_point = _numeric_point(side_quote)
                    if side_point is None:
                        continue
                    line_references.append(
                        {
                            "point": side_point,
                            "fair_probability": candidate_reference[
                                "fair_by_selection"
                            ][selection],
                            "overround": candidate_reference["overround"],
                            "reference": candidate_reference,
                        }
                    )

            candidate_point = _numeric_point(quote)
            if candidate_point is not None:
                line_pricing = estimate_line_adjusted_probability(
                    str(quote.get("sport") or ""),
                    key[2],
                    selection,
                    candidate_point,
                    line_references,
                    config,
                )
                if line_pricing is not None:
                    primary_line_reference = min(
                        line_pricing["source_references"],
                        key=lambda item: abs(item["point"] - candidate_point),
                    )
                    reference = primary_line_reference["reference"]

        if reference is None:
            reason = _drop_reason_for_missing_reference(key, reference_status)
            diagnostics["dropped"][reason] += 1
            continue

        ref_status = reference_status.get(key, {}) if line_pricing is None else {}
        if line_pricing is None and ref_status.get("status") != "valid":
            diagnostics["dropped"][
                ref_status.get("status", "no_pinnacle_reference")
            ] += 1
            continue
        if reference["overround"] > config.max_pinnacle_overround:
            diagnostics["dropped"]["overround_too_high"] += 1
            continue
        pinnacle_pair = list(reference["quotes"].values())
        if any(
            _quote_is_stale(item, now_utc, config.max_quote_age_seconds)
            for item in pinnacle_pair
        ) or _quote_is_stale(quote, now_utc, config.max_quote_age_seconds):
            diagnostics["dropped"]["stale_quote"] += 1
            continue

        selection = quote.get("selection")
        candidate_expected = set(quote.get("expected_outcomes") or [])
        if candidate_expected and candidate_expected != set(reference["selections"]):
            diagnostics["dropped"]["line_mismatch"] += 1
            continue
        fair_probability = (
            line_pricing["fair_probability"]
            if line_pricing is not None
            else reference["fair_by_selection"].get(selection)
        )
        pinnacle_side = reference["quotes"].get(selection)
        opposite_selection = next(
            (side for side in reference["selections"] if side != selection),
            None,
        )
        pinnacle_other = reference["quotes"].get(opposite_selection)
        if (
            fair_probability is None
            or pinnacle_side is None
            or pinnacle_other is None
        ):
            diagnostics["dropped"]["line_mismatch"] += 1
            continue
        if line_pricing is None and _base_market(quote) == "spreads":
            candidate_point = _numeric_point(quote)
            ref_point = _numeric_point(pinnacle_side)
            if (
                candidate_point is None
                or ref_point is None
                or abs(candidate_point - ref_point) > 1e-6
            ):
                diagnostics["dropped"]["line_mismatch"] += 1
                continue

        try:
            decimal_odds = float(quote["odds_decimal"])
            fee = _book_fee_fraction(str(quote.get("sportsbook") or ""), config)
            ev_decimal = net_decimal_odds(decimal_odds, fee)
            if line_pricing is None:
                win_probability = fair_probability
                lose_probability = 1 - fair_probability
                push_probability = 0.0
                market = _base_market(quote)
                exact_point = _numeric_point(quote)
                if (
                    market in {"spreads", "totals"}
                    and exact_point is not None
                    and _is_whole_line(exact_point)
                ):
                    standard_deviation = _standard_deviation_for_sport(
                        str(quote.get("sport") or ""), market, config
                    )
                    if standard_deviation is not None:
                        (
                            win_probability,
                            lose_probability,
                            push_probability,
                        ) = _normal_outcome_probabilities(
                            fair_probability, standard_deviation, True
                        )
                ev_pct = (
                    expected_value_with_push_pct(
                        ev_decimal, win_probability, lose_probability
                    )
                    if push_probability > 0
                    else expected_value_pct(ev_decimal, fair_probability)
                )
            else:
                win_probability = line_pricing["win_probability"]
                lose_probability = line_pricing["lose_probability"]
                push_probability = line_pricing["push_probability"]
                ev_pct = expected_value_with_push_pct(
                    ev_decimal, win_probability, lose_probability
                )
        except (KeyError, TypeError, ValueError):
            diagnostics["dropped"]["invalid_pinnacle_market"] += 1
            continue

        # Report both the headline EV at the quoted price and the EV at the
        # odds that can actually be filled for a normal-sized bet. Eligibility
        # stays on the quoted EV; executable EV is used for ranking/display so
        # thin books cannot outrank deeper ones on an unfillable price.
        execution = executable_odds_for_stake(
            quote, config.executable_odds_probe_stake
        )
        executable_ev_pct = ev_pct
        if execution["executable_decimal"] > 1:
            try:
                exec_ev_decimal = net_decimal_odds(
                    execution["executable_decimal"], fee
                )
                executable_ev_pct = (
                    expected_value_with_push_pct(
                        exec_ev_decimal, win_probability, lose_probability
                    )
                    if push_probability > 0
                    else expected_value_pct(exec_ev_decimal, fair_probability)
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        if execution["levels_used"]:
            diagnostics["liquidity_slippage_applied"] += 1
        if execution["size_unknown"]:
            diagnostics["liquidity_size_unknown"] += 1

        if ev_pct < config.min_ev_pct:
            diagnostics["dropped"]["below_min_ev"] += 1
            continue

        source_references = (
            line_pricing["source_references"]
            if line_pricing is not None
            else [
                {
                    "point": _numeric_point(pinnacle_side),
                    "reference": reference,
                }
            ]
        )
        source_lines = sorted(
            {item["point"] for item in source_references if item["point"] is not None}
        )
        line_adjustment_method = (
            line_pricing["method"] if line_pricing is not None else ""
        )
        flags = []
        if line_pricing is not None:
            flags.append("line-adjusted")
        if ev_pct > config.verify_ev_pct:
            flags.append("verify")
        row = {
            **quote,
            "market_key": _base_market(quote),
            "fair_probability": fair_probability,
            "win_probability": win_probability,
            "lose_probability": lose_probability,
            "push_probability": push_probability,
            "ev_pct": ev_pct,
            "ev_per_100": ev_pct,
            "odds_decimal_executable": execution["executable_decimal"],
            "odds_american_executable": (
                _american_from_decimal(execution["executable_decimal"])
                if execution["executable_decimal"] > 1
                else None
            ),
            "executable_ev_pct": executable_ev_pct,
            "execution_levels_used": execution["levels_used"],
            "execution_slippage_pct": execution["slippage_pct"],
            "execution_size_unknown": execution["size_unknown"],
            "pinnacle_american_this_side": pinnacle_side.get("odds_american")
            or _american_from_decimal(float(pinnacle_side["odds_decimal"])),
            "pinnacle_american_other_side": pinnacle_other.get("odds_american")
            or _american_from_decimal(float(pinnacle_other["odds_decimal"])),
            "pinnacle_selection_other_side": opposite_selection,
            "pinnacle_overround": reference["overround"],
            "pinnacle_quote_time": _quote_time_text(pinnacle_side),
            "book_quote_time": _quote_time_text(quote),
            "line": quote.get("point"),
            "pinnacle_line": _numeric_point(pinnacle_side),
            "pinnacle_lines_used": ", ".join(
                f"{point:g}" for point in source_lines
            ),
            "pricing_method": (
                "line-adjusted" if line_pricing is not None else "exact"
            ),
            "odds_decimal_net": ev_decimal,
            "line_adjustment_method": line_adjustment_method,
            "flags": ", ".join(flags),
            "reference": "Pinnacle",
        }
        if line_pricing is not None:
            row["kelly_fraction"] = (
                config.effective_line_adjusted_kelly_fraction
            )
        candidate_rows.append(row)

    opportunities, duplicate_count = deduplicate_alt_line_ladders(candidate_rows)
    diagnostics["dropped"]["alt_line_duplicate"] = duplicate_count
    diagnostics["rows_kept"] = len(opportunities)
    diagnostics["untimestamped_quotes_used"] = sum(
        (row["book_quote_time"] is None) + (row["pinnacle_quote_time"] is None)
        for row in opportunities
    )
    if opportunities:
        ev_values = [row["ev_pct"] for row in opportunities]
        diagnostics["ev_pct"] = {
            "min": min(ev_values),
            "median": median(ev_values),
            "max": max(ev_values),
        }
    return {
        "opportunities": opportunities,
        "eligible_rows": sorted(
            candidate_rows, key=lambda row: row["ev_pct"], reverse=True
        ),
        "pinnacle_snapshots": pinnacle_snapshots,
        "diagnostics": diagnostics,
    }


def _compatible_market_lines(
    market_key: str,
    outcome_quotes: dict[str, dict[str, Any]],
    outcomes: set[str],
) -> bool:
    market_type, separator, line_identity = market_key.partition(":")
    if not separator:
        return market_type not in {"spreads", "totals"}
    try:
        expected_line = float(line_identity.split(":", 1)[0])
    except ValueError:
        return False
    if not isfinite(expected_line):
        return False

    if market_key.startswith("spreads"):
        if len(outcomes) != 2:
            return False
        try:
            points = [float(outcome_quotes[outcome]["point"]) for outcome in outcomes]
        except (KeyError, TypeError, ValueError):
            return False
        return (
            all(isfinite(point) and point != 0 for point in points)
            and all(abs(abs(point) - expected_line) <= 1e-6 for point in points)
            and points[0] * points[1] < 0
            and abs(points[0] + points[1]) <= 1e-6
        )
    if market_key.startswith("totals"):
        try:
            points = [float(outcome_quotes[outcome]["point"]) for outcome in outcomes]
        except (KeyError, TypeError, ValueError):
            return False
        return (
            bool(points)
            and all(isfinite(point) for point in points)
            and all(abs(point - expected_line) <= 1e-6 for point in points)
            and max(points) - min(points) <= 1e-6
        )
    return True


def _spread_line_from_selection(selection: Any) -> float | None:
    if not isinstance(selection, str):
        return None
    for token in reversed(selection.split()):
        try:
            value = float(token)
        except (TypeError, ValueError):
            continue
        if isfinite(value):
            return value
    return None


def market_integrity(candidate: dict[str, Any]) -> tuple[bool, str]:
    """Verify the two legs are opposite, mutually exclusive outcomes."""
    market = str(candidate.get("market") or "").casefold()
    leg1 = candidate.get("leg_1") or {}
    leg2 = candidate.get("leg_2") or {}
    selection1 = str(leg1.get("selection") or "").strip()
    selection2 = str(leg2.get("selection") or "").strip()
    if not selection1 or not selection2:
        return False, "Both selections must be present."
    if market.startswith("spread"):
        line1 = _spread_line_from_selection(selection1)
        line2 = _spread_line_from_selection(selection2)
        if line1 is None or line2 is None:
            return False, "Could not parse spread lines from the selections."
        if line1 == 0 or line2 == 0 or line1 != -line2:
            return (
                False,
                f"Selections are not opposite equal-magnitude spreads "
                f"({line1:g} vs {line2:g}).",
            )
        return True, f"Opposite spreads of equal magnitude ({line1:g} / {line2:g})."
    if selection1.casefold() == selection2.casefold():
        return False, "Selections are identical, not mutually exclusive."
    return True, "Distinct selections on the same market."


def break_even_exchange_odds(leg1_decimal_odds: float) -> float:
    """Minimum exchange decimal odds that keep the arb non-negative."""
    if not isfinite(leg1_decimal_odds) or leg1_decimal_odds <= 1:
        return float("inf")
    return leg1_decimal_odds / (leg1_decimal_odds - 1.0)


def exchange_effective_odds(
    levels: list[dict[str, Any]], stake: float, fee_pct: float = 0.0
) -> tuple[float | None, float]:
    """Volume-weighted exchange decimal odds for ``stake``, after fees."""
    if not isfinite(stake) or stake <= 0:
        return None, 0.0
    ordered = sorted(
        levels, key=lambda level: float(level.get("decimal_odds", 0.0)), reverse=True
    )
    remaining = float(stake)
    weighted_price = 0.0
    filled = 0.0
    for level in ordered:
        if remaining <= 1e-9:
            break
        price = float(level.get("decimal_odds", 0.0))
        available = float(level.get("available_stake", 0.0))
        if not isfinite(price) or price <= 1 or not isfinite(available) or available <= 0:
            continue
        take = min(remaining, available)
        weighted_price += take * price
        filled += take
        remaining -= take
    if filled <= 0:
        return None, 0.0
    gross = weighted_price / filled
    net = 1.0 + (gross - 1.0) * (1.0 - fee_pct)
    return net, filled


def evaluate_arbitrage_opportunity(candidate: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a candidate arb with order-book depth and execution risk."""
    leg1 = candidate.get("leg_1") or {}
    leg2 = candidate.get("leg_2") or {}
    constraints = candidate.get("constraints") or {}
    levels = list(leg2.get("order_book_asks_or_implied") or [])

    min_depth = float(constraints.get("min_depth_dollars", 0.0))
    max_stake_per_bet = float(constraints.get("max_stake_per_bet", float("inf")))
    bankroll_cap = float(constraints.get("bankroll_cap", float("inf")))
    min_profit = float(constraints.get("min_profit_dollars", 0.0))
    min_roi = float(constraints.get("min_roi_pct", 0.0))
    fee_pct = float(leg2.get("exchange_fee_pct", 0.0))

    integrity_ok, integrity_msg = market_integrity(candidate)

    result: dict[str, Any] = {
        "sport": candidate.get("sport"),
        "matchup": candidate.get("matchup"),
        "market": candidate.get("market"),
        "market_integrity": {"ok": integrity_ok, "message": integrity_msg},
        "status": None,
        "effective_odds_fees": {},
        "binding_bottleneck": None,
        "recommended_stakes_payouts": {},
        "partial_fill_warning": "",
    }

    if not integrity_ok:
        result["status"] = "INVALID"
        result["partial_fill_warning"] = integrity_msg
        return result

    try:
        odds1 = float(leg1.get("decimal_odds"))
    except (TypeError, ValueError):
        result["status"] = "INVALID"
        result["partial_fill_warning"] = "Leg 1 decimal odds are invalid."
        return result
    if not isfinite(odds1) or odds1 <= 1:
        result["status"] = "INVALID"
        result["partial_fill_warning"] = "Leg 1 decimal odds are invalid."
        return result

    sportsbook_max = float(leg1.get("max_allowed_stake", max_stake_per_bet))
    break_even = break_even_exchange_odds(odds1)
    depth_at_be = sum(
        float(level.get("available_stake", 0.0))
        for level in levels
        if isfinite(float(level.get("decimal_odds", 0.0)))
        and float(level.get("decimal_odds", 0.0)) >= break_even - 1e-9
    )

    top_level_odds = max(
        (float(level.get("decimal_odds", 0.0)) for level in levels), default=0.0
    )
    top_level_net_odds = (
        1.0 + (top_level_odds - 1.0) * (1.0 - fee_pct) if top_level_odds > 1 else top_level_odds
    )
    top_level_depth = sum(
        float(level.get("available_stake", 0.0))
        for level in levels
        if isfinite(float(level.get("decimal_odds", 0.0)))
        and abs(float(level.get("decimal_odds", 0.0)) - top_level_odds) <= 1e-9
    )

    result["effective_odds_fees"] = {
        "leg1_headline_decimal_odds": odds1,
        "leg2_headline_decimal_odds": top_level_odds,
        "break_even_exchange_odds": break_even,
        "exchange_fee_pct": fee_pct,
        "depth_at_or_above_break_even": depth_at_be,
        "min_depth_dollars": min_depth,
    }

    if depth_at_be < min_depth - 1e-9:
        result["status"] = "REJECTED"
        result["binding_bottleneck"] = (
            f"Exchange depth at/above break-even is ${depth_at_be:,.2f}, "
            f"below min_depth_dollars ${min_depth:,.2f}."
        )
        result["partial_fill_warning"] = (
            "Insufficient executable depth; do not place the sportsbook leg."
        )
        return result

    max_s2 = min(depth_at_be, max_stake_per_bet, bankroll_cap)
    # Search the largest exchange stake (in cents) that clears every constraint.
    best: tuple[float, float, float, float, float, float, float] | None = None
    cents_max = int(round(max_s2 * 100.0))
    for cents in range(0, cents_max + 1):
        s2 = cents / 100.0
        if s2 <= 0:
            continue
        net_odds, filled = exchange_effective_odds(levels, s2, fee_pct)
        if net_odds is None or filled + 1e-9 < s2:
            continue
        s1 = s2 * net_odds / odds1
        total = s1 + s2
        profit = s2 * net_odds - total
        roi = (profit / total * 100.0) if total > 0 else 0.0
        if (
            s1 <= sportsbook_max + 1e-9
            and s1 <= max_stake_per_bet + 1e-9
            and s2 <= max_stake_per_bet + 1e-9
            and total <= bankroll_cap + 1e-9
            and profit >= min_profit - 1e-9
            and roi >= min_roi - 1e-9
        ):
            best = (s2, s1, total, profit, roi, net_odds, filled)

    # Identify the binding constraint for reporting.
    constraint_values = {
        "exchange_depth_at_break_even": depth_at_be,
        "max_stake_per_bet": max_stake_per_bet,
        "bankroll_cap": bankroll_cap,
        "sportsbook_max_allowed_stake": (
            sportsbook_max * odds1 / top_level_odds if top_level_odds > 1 else sportsbook_max
        ),
    }
    binding_name = min(constraint_values, key=lambda name: constraint_values[name])
    binding_value = constraint_values[binding_name]

    # Worst-case partial fill: Leg 1 sized for the target, only top level fills.
    target_s2 = best[0] if best is not None else max_s2
    if best is not None:
        target_net_odds = best[5]
    else:
        target_net_odds = exchange_effective_odds(levels, max_s2, fee_pct)[0]
        if target_net_odds is None:
            target_net_odds = top_level_odds
    target_s1 = target_s2 * target_net_odds / odds1 if target_s2 > 0 else 0.0
    partial_fill_qty = min(top_level_depth, target_s2)
    partial_outlay = target_s1 + partial_fill_qty
    partial_payout = min(target_s1 * odds1, partial_fill_qty * top_level_net_odds)
    worst_case_loss = partial_outlay - partial_payout

    result["binding_bottleneck"] = (
        f"{binding_name} at ${binding_value:,.2f} "
        f"(exchange depth usable ${depth_at_be:,.2f})."
    )

    if best is None:
        result["status"] = "REJECTED"
        max_depth_odds, _ = exchange_effective_odds(levels, max_s2, fee_pct)
        if max_depth_odds is None:
            max_depth_odds = 0.0
        max_achievable_profit = max_s2 * max_depth_odds - (
            max_s2 * max_depth_odds / odds1 + max_s2
        )
        if max_achievable_profit < 0:
            max_achievable_profit = 0.0
        result["recommended_stakes_payouts"] = {
            "executable_exchange_stake": 0.0,
            "sportsbook_stake": 0.0,
            "total_stake": 0.0,
            "locked_payout": 0.0,
            "net_guaranteed_profit": 0.0,
            "roi_pct": 0.0,
            "max_achievable_profit": round(max_achievable_profit, 2),
        }
        result["partial_fill_warning"] = (
            f"No stake clears min_profit_dollars ${min_profit:,.2f} and "
            f"min_roi_pct {min_roi:.2f}%. Best available depth yields at most "
            f"${max_achievable_profit:,.2f} "
            f"guaranteed profit under the binding constraints. Leg 1 must not be placed. If it were placed for "
            f"${target_s1:,.2f} and the hedge only partially filled "
            f"({partial_fill_qty:,.2f} @ {top_level_odds:.4f}), worst-case loss "
            f"is ${worst_case_loss:,.2f}."
        )
        return result

    s2, s1, total, profit, roi, net_odds, filled = best
    result["status"] = "INSTANT_TAKE"
    result["effective_odds_fees"]["volume_weighted_exchange_odds"] = net_odds
    result["effective_odds_fees"]["exchange_filled_stake"] = filled
    result["recommended_stakes_payouts"] = {
        "exchange_leg_stake": round(s2, 2),
        "sportsbook_leg_stake": round(s1, 2),
        "total_stake": round(total, 2),
        "locked_payout": round(s2 * net_odds, 2),
        "net_guaranteed_profit": round(profit, 2),
        "roi_pct": round(roi, 4),
    }
    result["partial_fill_warning"] = (
        f"Instant fill available across {len(levels)} level(s). If Leg 1 is locked "
        f"and the hedge partially fills to only {partial_fill_qty:,.2f} @ "
        f"{top_level_odds:.4f}, worst-case loss is ${worst_case_loss:,.2f}. "
        f"Resting a limit order is not required for the recommended size."
    )
    return result


def format_arbitrage_evaluation(result: dict[str, Any]) -> str:
    """Render an evaluation dict as the required concise breakdown."""
    lines = []
    lines.append(f"Status: {result.get('status')}")
    eff = result.get("effective_odds_fees", {})
    lines.append(
        "Effective Odds & Fees: "
        f"Leg1 {eff.get('leg1_headline_decimal_odds')} vs Leg2 headline "
        f"{eff.get('leg2_headline_decimal_odds')}; break-even exchange "
        f"{eff.get('break_even_exchange_odds')}; volume-weighted "
        f"{eff.get('volume_weighted_exchange_odds', 'n/a')}; "
        f"fee {eff.get('exchange_fee_pct', 0) * 100:.2f}%; "
        f"depth at/above break-even ${eff.get('depth_at_or_above_break_even', 0):,.2f} "
        f"(min ${eff.get('min_depth_dollars', 0):,.2f})."
    )
    lines.append(f"Binding Liquidity Bottleneck: {result.get('binding_bottleneck')}")
    rec = result.get("recommended_stakes_payouts", {})
    lines.append(
        "Recommended Stakes & Payouts: "
        f"exchange ${rec.get('exchange_leg_stake', rec.get('executable_exchange_stake', 0)):,.2f}, "
        f"sportsbook ${rec.get('sportsbook_leg_stake', rec.get('sportsbook_stake', 0)):,.2f}, "
        f"total ${rec.get('total_stake', 0):,.2f}, "
        f"locked payout ${rec.get('locked_payout', 0):,.2f}, "
        f"net profit ${rec.get('net_guaranteed_profit', 0):,.2f}, "
        f"ROI {rec.get('roi_pct', 0):.4f}%."
    )
    lines.append(f"Partial-Fill / Execution Warning: {result.get('partial_fill_warning')}")
    return "\n".join(lines)


def _allocate_rounded_stakes(
    odds: list[float], total_stake: int, rounding_step: int, target_stakes: list[float]
) -> list[int] | None:
    budget_units, remainder = divmod(total_stake, rounding_step)
    remainder_options = [None] if remainder == 0 else range(len(odds))
    allocations = []

    for remainder_index in remainder_options:
        def required_units(target_cents: int) -> list[int]:
            target_dollars = target_cents / 100
            units = []
            for index, price in enumerate(odds):
                extra_stake = remainder if index == remainder_index else 0
                minimum_units = 0 if extra_stake else 1
                units_needed = ceil(
                    (target_dollars - extra_stake * price) / (rounding_step * price)
                    - 1e-10
                )
                units.append(max(minimum_units, units_needed))
            return units

        low = 0
        high = int(total_stake * min(odds) * 100)
        while low < high:
            target = (low + high + 1) // 2
            if sum(required_units(target)) <= budget_units:
                low = target
            else:
                high = target - 1

        units = required_units(low)
        if sum(units) > budget_units:
            continue
        stakes = [
            count * rounding_step
            + (remainder if index == remainder_index else 0)
            for index, count in enumerate(units)
        ]
        unused_units = budget_units - sum(units)
        while unused_units:
            lowest_payout = min(
                range(len(odds)), key=lambda index: stakes[index] * odds[index]
            )
            stakes[lowest_payout] += rounding_step
            unused_units -= 1
        payouts = [stake * price for stake, price in zip(stakes, odds)]
        distance_from_target = sum(
            abs(stake - target) for stake, target in zip(stakes, target_stakes)
        )
        allocations.append((min(payouts), -distance_from_target, stakes))

    if not allocations:
        return None
    return max(allocations, key=lambda allocation: (allocation[0], allocation[1]))[2]


def _rounded_arbitrage_result(
    odds: list[float], total_stake: int, rounding_step: int
) -> tuple[list[int], float, float] | None:
    """Allocate a whole-dollar total and return (stakes, profit, locked payout)."""
    if total_stake < 2 or not odds:
        return None
    implied_probability_sum = sum(1 / price for price in odds)
    if (
        not isfinite(implied_probability_sum)
        or implied_probability_sum >= 1
    ):
        return None
    raw_stakes = [
        total_stake * (1 / price) / implied_probability_sum for price in odds
    ]
    stakes = _allocate_rounded_stakes(
        odds, total_stake, rounding_step, raw_stakes
    )
    if stakes is None:
        return None
    payouts = [stake * price for stake, price in zip(stakes, odds)]
    profit = round(min(payouts) - total_stake, 2)
    return stakes, profit, min(payouts)


def size_arbitrage_stakes(
    odds: list[float],
    total_stake: float,
    rounding_step: int = 1,
    max_leg_stake: float | None = None,
) -> tuple[int, list[int], float, float] | None:
    """
    Find the largest whole-dollar total that respects the per-leg limit.

    The total is normally the requested stake. When ``max_leg_stake`` is
    supplied, the total is scaled down only as far as needed so that every leg
    stays within the limit. The existing whole-dollar rounding increment is
    applied by ``_allocate_rounded_stakes`` and the result is re-verified to be
    positive.
    """
    prices = [float(price) for price in odds]
    if not prices or not all(isfinite(price) and price > 1 for price in prices):
        return None
    total = int(total_stake)
    if total < 2:
        return None
    if (
        isinstance(rounding_step, bool)
        or not isinstance(rounding_step, int)
        or rounding_step < 1
    ):
        raise ValueError("Rounding step must be a positive whole-dollar amount.")
    if max_leg_stake is not None and (
        not isfinite(max_leg_stake) or max_leg_stake < 1
    ):
        raise ValueError("Maximum arbitrage leg stake must be at least one dollar.")

    def attempt(candidate: int) -> tuple[str, tuple[list[int], float, float] | None]:
        outcome = _rounded_arbitrage_result(prices, candidate, rounding_step)
        if outcome is None:
            return "low", None
        stakes, profit, locked_payout = outcome
        if profit <= 0:
            return "low", None
        if max_leg_stake is not None and any(
            stake > max_leg_stake for stake in stakes
        ):
            return "high", None
        return "ok", (stakes, profit, locked_payout)

    status, outcome = attempt(total)
    if status == "ok" and outcome is not None:
        return (total, *outcome)

    best: tuple[int, list[int], float, float] | None = None
    low, high = 2, total - 1
    while low <= high:
        middle = (low + high) // 2
        status, outcome = attempt(middle)
        if status == "ok" and outcome is not None:
            best = (middle, *outcome)
            low = middle + 1
        elif status == "high":
            high = middle - 1
        else:
            low = middle + 1

    if best is not None:
        return best

    # Rounding increments can make the profit function non-monotonic for very
    # small totals. Fall back to a short descending search to guarantee that a
    # feasible size is not missed.
    for candidate in range(total - 1, 1, -1):
        status, outcome = attempt(candidate)
        if status == "ok" and outcome is not None:
            return (candidate, *outcome)
    return None


def find_arbitrage_opportunities(
    quotes: list[dict[str, Any]],
    total_stake: float,
    rounding_step: int = 1,
    config=CONFIG,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if not isfinite(total_stake) or total_stake <= 0:
        raise ValueError("Total stake must be greater than zero.")
    if not float(total_stake).is_integer():
        raise ValueError("Total stake must be a whole-dollar amount.")
    if (
        isinstance(rounding_step, bool)
        or not isinstance(rounding_step, int)
        or rounding_step < 1
    ):
        raise ValueError("Rounding step must be a positive whole-dollar amount.")
    total_stake = int(total_stake)
    total_stake = min(
        total_stake,
        int(getattr(config, "max_arb_total_stake_dollars", total_stake)),
    )
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)

    # Resolve partial player identifiers across books: prefer player_id, and
    # fall back to the normalized description only when an outcome lacks one.
    player_aliases: dict[tuple[Any, ...], str] = {}
    for quote in quotes:
        if not is_player_prop_quote(quote):
            continue
        player_id = quote.get("player_id")
        player_name = _normalize_player_name(quote.get("player_name"))
        point = _numeric_point(quote)
        base_market = str(
            quote.get("market_key") or quote.get("market") or ""
        ).split(":", 1)[0]
        if player_id is not None and str(player_id).strip() and player_name:
            alias_key = (
                quote.get("sport", ""),
                quote.get("event_id", ""),
                base_market,
                quote.get("period", ""),
                point,
                player_name,
            )
            player_aliases[alias_key] = f"id:{str(player_id).strip()}"

    markets: dict[
        tuple[str, str, str, str, str, float | None],
        dict[str, list[dict[str, Any]]],
    ] = defaultdict(lambda: defaultdict(list))
    for quote in quotes:
        if _quote_time(quote) is None or _quote_is_stale(
            quote, now_utc, config.max_quote_age_seconds
        ):
            continue
        # Pinnacle is strictly a sharp reference and can never be an arb leg.
        if _is_pinnacle_quote(quote, config):
            continue
        if _is_milestone_quote(quote):
            continue
        sportsbook = str(quote.get("sportsbook") or "").strip()
        if not sportsbook:
            continue
        odds = float(quote["odds_decimal"])
        if not isfinite(odds) or odds <= 1:
            continue

        # NCAAB efficiency filters: skip player props, alternate lines, and
        # thin markets on the noisiest sport.
        if str(quote.get("sport") or "").casefold() == "ncaab":
            if is_player_prop_quote(quote):
                continue
            line_type = str(quote.get("line_type") or "").casefold()
            if line_type == "alternate":
                continue
            liquidity = quote.get("liquidity")
            if liquidity is not None and not isinstance(liquidity, bool):
                try:
                    liq = float(liquidity)
                except (TypeError, ValueError):
                    liq = None
                if (
                    liq is not None
                    and isfinite(liq)
                    and liq < getattr(config, "ncaab_min_liquidity_dollars", config.min_liquidity_dollars)
                ):
                    continue

        player_prop = is_player_prop_quote(quote)
        player_scope = ""
        point = _numeric_point(quote)
        market_key_text = str(
            quote.get("market_key") or quote.get("market") or ""
        )
        if player_prop:
            base_market = market_key_text.split(":", 1)[0]
            player_name = _normalize_player_name(quote.get("player_name"))
            alias_key = (
                quote.get("sport", ""),
                quote.get("event_id", ""),
                base_market,
                quote.get("period", ""),
                point,
                player_name,
            )
            player_scope = player_aliases.get(alias_key) or _player_identity(quote)
            grouping_market_key = base_market
        else:
            grouping_market_key = market_key_text

        market_key = (
            quote["sport"],
            quote.get("event_id", quote["matchup"]),
            grouping_market_key,
            quote.get("period", ""),
            player_scope,
            round(point, 6) if player_prop and point is not None else None,
        )
        markets[market_key][quote["selection"]].append(quote)

    opportunities = []
    for (
        sport,
        event_id,
        market_key,
        period,
        player_scope,
        player_point,
    ), selection_quotes in markets.items():
        if len(selection_quotes) != 2:
            continue
        base_market_type = str(market_key).split(":", 1)[0].casefold()
        player_prop = bool(player_scope) or is_player_prop_market_key(
            market_key
        )
        if player_prop:
            # Props require the same player's Over and Under at one line.
            # Milestone outcomes can share the feed market, so do not let their
            # declared outcome list block a valid Over/Under pair.
            if not player_scope:
                continue
            if set(selection_quotes) != {"Over", "Under"}:
                continue
            points = [
                _numeric_point(item)
                for quotes_for_selection in selection_quotes.values()
                for item in quotes_for_selection
            ]
            if any(value is None for value in points):
                continue
            if max(points) - min(points) > 1e-6:  # type: ignore[type-var]
                continue
            if player_point is None:
                continue
            expected_outcomes = set(selection_quotes)
        else:
            group_quotes = [
                quote
                for quotes_for_selection in selection_quotes.values()
                for quote in quotes_for_selection
            ]
            declared_outcomes = [
                set(quote["expected_outcomes"])
                for quote in group_quotes
                if quote.get("expected_outcomes")
            ]
            # Every book that declares its outcomes must describe the same
            # two-way market. This rejects three-way moneylines (which include
            # a draw even if the draw quote is missing from this snapshot).
            if any(
                len(outcomes) != 2 or outcomes != set(selection_quotes)
                for outcomes in declared_outcomes
            ):
                continue
            expected_outcomes = (
                declared_outcomes[0] if declared_outcomes else None
            )
            if base_market_type == "totals":
                # Totals require the same line's Over and Under.
                if set(selection_quotes) != {"Over", "Under"}:
                    continue
            elif base_market_type != "spreads":
                # Moneyline must be a true two-way market. A draw selection
                # means this is a three-way market and cannot be covered by
                # two legs.
                if any(
                    str(selection).strip().casefold() == "draw"
                    for selection in selection_quotes
                ):
                    continue

        selection_names = list(expected_outcomes or selection_quotes)
        first_quotes = selection_quotes[selection_names[0]]
        second_quotes = selection_quotes[selection_names[1]]
        compatible_pairs = []
        for first_quote in first_quotes:
            for second_quote in second_quotes:
                if _is_pinnacle_quote(first_quote, config) or _is_pinnacle_quote(
                    second_quote, config
                ):
                    continue
                if (
                    str(first_quote["sportsbook"]).casefold()
                    == str(second_quote["sportsbook"]).casefold()
                ):
                    continue
                market_type_text = str(market_key)
                if market_type_text.startswith("spreads"):
                    if not _compatible_market_lines(
                        market_type_text,
                        {
                            selection_names[0]: first_quote,
                            selection_names[1]: second_quote,
                        },
                        set(selection_names),
                    ):
                        continue
                elif market_type_text.startswith(
                    "totals"
                ) and not _compatible_market_lines(
                    market_type_text,
                    {
                        selection_names[0]: first_quote,
                        selection_names[1]: second_quote,
                    },
                    set(selection_names),
                ):
                    continue
                odds_sum = (
                    1 / float(first_quote["odds_decimal"])
                    + 1 / float(second_quote["odds_decimal"])
                )
                compatible_pairs.append((odds_sum, first_quote, second_quote))
        if not compatible_pairs:
            continue
        implied_probability_sum, first_quote, second_quote = min(
            compatible_pairs, key=lambda pair: pair[0]
        )
        if implied_probability_sum >= 1 or total_stake < 2:
            continue

        selection_rows = [
            (selection_names[0], first_quote),
            (selection_names[1], second_quote),
        ]
        odds = [float(quote["odds_decimal"]) for _, quote in selection_rows]
        sizing = size_arbitrage_stakes(odds, total_stake, rounding_step)
        if sizing is None:
            continue
        sized_total, stakes, profit, guaranteed_payout = sizing
        if profit < config.min_arb_guaranteed_profit_dollars - 1e-9:
            continue

        legs = []
        for stake, (selection, quote) in zip(stakes, selection_rows):
            leg_odds = float(quote["odds_decimal"])
            legs.append(
                {
                    "selection": selection,
                    "market": quote["market"],
                    "sportsbook": quote["sportsbook"],
                    "player_id": quote.get("player_id"),
                    "player_name": quote.get("player_name"),
                    "line": quote.get("point"),
                    "odds_decimal": leg_odds,
                    "odds_american": quote.get("odds_american"),
                    "liquidity": quote.get("liquidity"),
                    "liquidity_updated_at": quote.get("liquidity_updated_at"),
                    "stake": stake,
                    "payout": round(stake * leg_odds, 2),
                }
            )

        opportunities.append(
            {
                "sport": sport,
                "matchup": first_quote["matchup"],
                "kickoff": first_quote.get("kickoff", ""),
                "market": first_quote["market"],
                "event_id": event_id,
                "market_key": market_key,
                "period": period,
                "is_player_prop": player_prop,
                "player_id": first_quote.get("player_id")
                or second_quote.get("player_id"),
                "player_name": first_quote.get("player_name")
                or second_quote.get("player_name"),
                "line": first_quote.get("point")
                if first_quote.get("point") is not None
                else second_quote.get("point"),
                "implied_probability_sum": implied_probability_sum,
                "theoretical_roi_pct": (1 / implied_probability_sum - 1) * 100,
                "theoretical_profit": sized_total
                * (1 / implied_probability_sum - 1),
                "theoretical_locked_payout": sized_total
                / implied_probability_sum,
                "roi_pct": profit / sized_total * 100,
                "total_stake": sized_total,
                "requested_total_stake": total_stake,
                "min_guaranteed_profit": config.min_arb_guaranteed_profit_dollars,
                "locked_payout": round(guaranteed_payout, 2),
                "profit": profit,
                "legs": legs,
            }
        )
    return sorted(opportunities, key=lambda item: item["roi_pct"], reverse=True)
