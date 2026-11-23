from math import floor, isfinite
from typing import Any

from scanner_config import CONFIG
from scanner_logic import devig_two_way


def is_player_prop_market(market_key: Any) -> bool:
    """Player props are arbitrage-only and never eligible for +EV sizing."""
    return str(market_key or "").split(":", 1)[0].casefold().startswith(
        "player_"
    )


def is_player_prop_quote(quote: dict[str, Any]) -> bool:
    return quote.get("is_player_prop") is True or is_player_prop_market(
        quote.get("market_key") or quote.get("market")
    )


def decimal_to_american(decimal: float) -> int:
    if decimal >= 2.0:
        return int(round((decimal - 1.0) * 100))
    return int(round(-100.0 / (decimal - 1.0)))


def remove_vig(odds_list: list) -> list:
    implied_probs = [1.0 / odds for odds in odds_list]
    total_ip = sum(implied_probs)
    return [ip / total_ip for ip in implied_probs]


def calculate_kelly(
    bankroll: float,
    fair_prob: float,
    decimal_odds: float,
    fraction: float = CONFIG.kelly_fraction,
) -> float:
    if (
        not isfinite(bankroll)
        or bankroll < 0
        or not isfinite(fair_prob)
        or not 0 <= fair_prob <= 1
        or not isfinite(decimal_odds)
        or decimal_odds <= 1
        or not isfinite(fraction)
        or not 0 <= fraction <= 1
    ):
        raise ValueError("Bankroll, probability, odds, and Kelly fraction are invalid.")
    full_kelly = (fair_prob * decimal_odds - 1) / (decimal_odds - 1)
    recommended = bankroll * max(0.0, full_kelly) * fraction
    return round(recommended, 2)


def _has_flag(flags: Any, name: str) -> bool:
    return name in {part.strip() for part in str(flags or "").split(",")}


def _reported_liquidity(quote: dict[str, Any]) -> float | None:
    liquidity = quote.get("liquidity")
    if liquidity is None or isinstance(liquidity, bool):
        return None
    try:
        value = float(liquidity)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _validate_stake_inputs(
    bankroll: float,
    fraction: float,
    per_bet_cap_fraction: float,
    per_event_cap_fraction: float,
    verify_stake_multiplier: float,
    liquidity_stake_fraction: float | None = None,
) -> None:
    if not isfinite(bankroll) or bankroll < 0:
        raise ValueError("Bankroll must be a nonnegative finite amount.")
    if not isfinite(fraction) or not 0 <= fraction <= 1:
        raise ValueError("Kelly fraction must be between 0 and 1.")
    if (
        not isfinite(per_bet_cap_fraction)
        or not 0 <= per_bet_cap_fraction <= 1
        or not isfinite(per_event_cap_fraction)
        or not 0 <= per_event_cap_fraction <= 1
    ):
        raise ValueError("Stake caps must be between 0 and 1.")
    if not isfinite(verify_stake_multiplier) or not 0 <= verify_stake_multiplier <= 1:
        raise ValueError("Verify stake multiplier must be between 0 and 1.")
    if liquidity_stake_fraction is not None and (
        not isfinite(liquidity_stake_fraction)
        or not 0 <= liquidity_stake_fraction <= 1
    ):
        raise ValueError("Liquidity stake fraction must be between 0 and 1.")


def _base_kelly_stakes(
    opportunities: list[dict[str, Any]],
    bankroll: float,
    fraction: float,
    per_bet_cap_fraction: float,
    verify_stake_multiplier: float,
) -> tuple[list[float], dict[tuple[str, str], list[int]]]:
    """Kelly stakes before event scaling or venue liquidity caps."""
    raw_stakes = []
    events: dict[tuple[str, str], list[int]] = {}
    for index, quote in enumerate(opportunities):
        if is_player_prop_quote(quote):
            raw_stakes.append(0.0)
            continue
        quote_fraction = quote.get("kelly_fraction", fraction)
        verify_multiplier = (
            verify_stake_multiplier if _has_flag(quote.get("flags"), "verify") else 1.0
        )
        raw_stake = min(
            calculate_kelly(
                bankroll,
                quote["fair_probability"],
                quote.get("odds_decimal_net", quote["odds_decimal"]),
                quote_fraction,
            )
            * verify_multiplier,
            bankroll * per_bet_cap_fraction,
        )
        raw_stakes.append(raw_stake)
        event_key = (
            quote.get("sport", ""),
            quote.get("event_id", quote.get("matchup", "")),
        )
        events.setdefault(event_key, []).append(index)
    return raw_stakes, events


def calculate_required_kelly_stakes(
    opportunities: list[dict[str, Any]],
    bankroll: float,
    fraction: float = CONFIG.kelly_fraction,
    per_bet_cap_fraction: float = CONFIG.max_bet_pct,
    per_event_cap_fraction: float = CONFIG.max_event_pct,
    verify_stake_multiplier: float = CONFIG.verify_stake_multiplier,
    max_single_bet: float | None = None,
) -> list[float]:
    """Stake each bet needs, after bankroll caps but BEFORE any liquidity cap.

    This is the number checked against an exchange's available size: capping it
    at a fraction of the quoted liquidity first would make that check circular.
    """
    _validate_stake_inputs(
        bankroll,
        fraction,
        per_bet_cap_fraction,
        per_event_cap_fraction,
        verify_stake_multiplier,
    )
    raw_stakes, events = _base_kelly_stakes(
        opportunities, bankroll, fraction, per_bet_cap_fraction,
        verify_stake_multiplier,
    )
    required = [0.0] * len(opportunities)
    for indices in events.values():
        event_total = sum(raw_stakes[index] for index in indices)
        event_cap = bankroll * per_event_cap_fraction
        scale = min(1.0, event_cap / event_total) if event_total else 0.0
        for index in indices:
            stake = raw_stakes[index] * scale
            if max_single_bet is not None:
                stake = min(stake, float(max_single_bet))
            required[index] = float(floor((stake + 1e-9) * 100) / 100)
    return required


def calculate_capped_kelly_stakes(
    opportunities: list[dict[str, Any]],
    bankroll: float,
    fraction: float,
    per_bet_cap_fraction: float = CONFIG.max_bet_pct,
    per_event_cap_fraction: float = CONFIG.max_event_pct,
    verify_stake_multiplier: float = CONFIG.verify_stake_multiplier,
    liquidity_stake_fraction: float = CONFIG.liquidity_stake_fraction,
) -> list[float]:
    _validate_stake_inputs(
        bankroll,
        fraction,
        per_bet_cap_fraction,
        per_event_cap_fraction,
        verify_stake_multiplier,
        liquidity_stake_fraction,
    )
    raw_stakes, events = _base_kelly_stakes(
        opportunities, bankroll, fraction, per_bet_cap_fraction,
        verify_stake_multiplier,
    )

    recommended = [0.0] * len(opportunities)
    for indices in events.values():
        event_total = sum(raw_stakes[index] for index in indices)
        event_cap = bankroll * per_event_cap_fraction
        scale = min(1.0, event_cap / event_total) if event_total else 0.0
        for index in indices:
            capped_stake = raw_stakes[index] * scale
            liquidity = _reported_liquidity(opportunities[index])
            if liquidity is not None:
                capped_stake = min(capped_stake, liquidity * liquidity_stake_fraction)
            recommended[index] = float(
                floor((capped_stake + 1e-9) * 100) / 100
            )
    return recommended


def bet_clv_pct(
    bet_decimal: float,
    closing_decimal: float | None,
    other_closing_decimal: float | None,
    method: str = CONFIG.devig_method,
) -> float:
    """No-vig CLV: book price times the de-vigged closing probability of the bet side."""
    if closing_decimal is None or other_closing_decimal is None:
        raise ValueError("Both sides' closing decimal odds are required for CLV.")
    fair_close, _ = devig_two_way(closing_decimal, other_closing_decimal, method)
    return (bet_decimal * fair_close - 1) * 100


def round_stake(
    stake: float, rounding_step: int = CONFIG.arbitrage_rounding_step
) -> float:
    return float(round(stake / rounding_step) * rounding_step)
