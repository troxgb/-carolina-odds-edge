# NC Odds Edge

A local dashboard for comparing betting-venue prices, estimating positive
expected value, finding cross-venue arbitrage, and tracking placed bets. Its
compact dark board takes general UI inspiration from odds-screen
products; it does not use OddsJam branding or assets and is not affiliated
with OddsJam.

## Live odds source

The app uses PropLine's API (`https://api.prop-line.com`) for real-time game
lines. Each selected sport is fetched with one bulk request for `h2h`, `spreads`,
and `totals`; the app does not poll automatically or make one request per event.
PropLine's free tier ignores the requested market list closely enough to return
every **alternate and milestone line** as well — 94% of each payload. Only main
lines (`line_type` of `main`, or rows with no line type) are kept by default via
`ScannerConfig.main_lines_only`, because alternate points are rarely comparable
across books and one NCAAF response alone was 41 MB / 83k quotes. Turning the
flag off restores alternate lines but needs far more than the 1 GB VPS this runs
on. Player-prop rows are additionally limited to the markets configured in
`player_prop_markets_by_sport`.
For NBA/NCAAB the same request also asks for `player_points`, `player_rebounds`,
and `player_assists`; for NFL/NCAAF it asks for `player_pass_yds`,
`player_rush_yds`, and `player_reception_yds`. Player props are used only to find
arbitrage (Over vs Under at the same line at two venues). They never receive a
Pinnacle +EV evaluation, are never shown on the +EV board, and are never written
to the Pinnacle +EV/CLV snapshot tables.
upcoming scans show events whose kickoff is still in the future in US Eastern
time, while live scans show events marked live and not completed by the API.
Event times are displayed in US Eastern time (`America/New_York`). Requests
include the configured North Carolina venues (DraftKings, FanDuel, BetMGM,
Fanatics, theScore Bet, Novig, and ProphetX) and Pinnacle. Pinnacle
is collected only as a sharp reference and never shown as a bettable venue.
No automatic retry is
made when a rate or daily quota limit is reached.
Each sport is fetched in its own HTTPS session, one sport at a time
(`api_max_workers=1`) so a single large payload never overlaps another in
memory. Direct PropLine connections use the host's IPv4 address because
the local IPv6 route was observed to fail TLS intermittently; hostname and
certificate validation remain enabled. Transient TLS handshake failures are
retried up to four times with exponential backoff, and connect/read timeouts
are retried once. A failed sport does not prevent the remaining sports from
being attempted.

## Run on Windows

From this directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

The app opens at `http://localhost:8501`. Add your API key in the sidebar's
masked **PropLine API key** field, or configure one or two keys in the server
environment before starting the app. The cron worker uses the dual-key
variables `PROPLINE_API_KEY_1` and `PROPLINE_API_KEY_2` and falls back to the
legacy `PROPLINE_API_KEY` when the numbered keys are absent. A server-configured
key is never put in a browser-side widget. Get a free key at
https://prop-line.com. Select sports and schedule, choose betting venues, then
click **Scan odds**. The app displays remaining daily quota when PropLine
includes it in the response headers.

The app is locked unless `APP_ACCESS_PASSWORD` is configured with at least 16
characters. Set a long, random shared passphrase and give it only to people
you intend to invite. In PowerShell, set both values before running Streamlit:

```powershell
$env:APP_ACCESS_PASSWORD = "your-long-random-shared-passphrase"
$env:PROPLINE_API_KEY = "your-key"
```

For Streamlit Community Cloud, add these values to the app's **Settings >
Secrets** instead of committing them:

```toml
APP_ACCESS_PASSWORD = "your-long-random-shared-passphrase"
PROPLINE_API_KEY = "your-key"
```

The passphrase grants access to the shared app and shared bet database; it does
not create separate private accounts for each friend. Rotate the passphrase if
you need to revoke access.

The free tier allows 1,000 requests per day per key. A scan uses at most one
request per selected sport (seven if all supported sports are selected).
Re-scan only when you want a refreshed snapshot; responses are not polled in
the background.

The worker rotates the two keys by UTC half-day: `PROPLINE_API_KEY_1` is primary
from 00:00-11:59 UTC and `PROPLINE_API_KEY_2` from 12:00-23:59 UTC. It records
`X-Daily-Remaining`, `X-Daily-Used`, and `X-Daily-Reset` per key (only a
non-reversible key fingerprint is stored) in `quota_state.json`, and it fails
over to the other key when the active key reports fewer than 15 requests left
or returns HTTP 429. Off-season sports with no upcoming/live events in the last
cached scan are skipped so they do not consume a credit.

## Cron worker

`worker.py` is a standalone one-shot pipeline for a local machine or VPS. It
reads credentials from the environment or a local `.env` file, makes one bulk
request per active sport, writes `cached_odds.json` and `alert_history.json`
atomically (`.tmp` plus `os.replace`), and stores scans/bets in `data/bets.db`
using SQLite transactions. It does not commit or push to Git, so it can run
outside version control on a schedule:

```cron
*/10 * * * * cd /opt/nc-odds-edge && /usr/bin/flock -n /tmp/odds_worker.lock timeout 300 /opt/nc-odds-edge/.venv/bin/python -u worker.py >> worker.log 2>&1
```

Use `flock` and `python -u` together: `flock` keeps two scans from running at
once, and `-u` keeps the log readable if a run is ever killed. A manual
`--dry-run` should take the same lock, otherwise it competes with a cron scan
for memory on a small VPS:

```bash
cd /opt/carolina-odds-edge && flock -w 300 /tmp/odds_worker.lock \
  .venv/bin/python -u worker.py --dry-run
```

Required environment variables:

```dotenv
PROPLINE_API_KEY_1=first-key
PROPLINE_API_KEY_2=second-key
TELEGRAM_BOT_TOKEN=123456:bot-token
TELEGRAM_CHAT_ID=-1001234567890
```

Optional variables include `WORKER_MIN_EV_PCT` (default `3.5`),
`WORKER_MAX_EV_PCT` (default uncapped; set to `none` or leave unset),
`WORKER_OFFSEASON_SPORTS` (comma-separated sport labels), `QUOTA_STATE_FILE`,
`CACHED_ODDS_FILE`, `ALERT_HISTORY_FILE`, `BETS_DB_PATH`, and
`DISCORD_WEBHOOK_URL` (optional fallback if Telegram is not configured). Run
`python worker.py --dry-run` to print alerts without sending them, or
`python worker.py --send-guide` to post the onboarding message.

If a Telegram group is upgraded to a supergroup, its numeric chat ID changes and
every send fails with HTTP 400 `group chat was upgraded to a supergroup chat`.
The error includes the new ID in `parameters.migrate_to_chat_id`; update
`TELEGRAM_CHAT_ID` to that value. `getChat` still answers for the old ID, so
confirm delivery with a real `sendMessage`, not `getChat`.

The +EV worker floor is 3.5% with no upper cap; large edges stay alertable and
still carry the existing high-EV "verify" flag. Kelly stakes are reported in
0.25u increments (1u = 1% of bankroll). Arbitrage alerts show both legs, venue,
odds, stake, locked payout, and player-prop details when applicable; an
opportunity is only alerted when its rounded stakes still guarantee at least
$10.00 in profit.

## Calculations and scan quality

- Free-tier PropLine enrichments (opt-in via `CONFIG.include_depth`,
  `include_book_ids`, `include_links`) can attach order-book depth, native book
  ids, and bet links without extra quota. Depth is available for ProphetX among
  our approved books and feeds the arbitrage evaluator's execution-mode
  classification.
- Totals are grouped by line, period, and team. A game total is never compared
  with either team's total; team-total rows show the team in the market label.
- Duplicate market generations use the newest `last_update`. Outcomes whose
  `last_seen_at` predates that market update are excluded as no longer offered.
- Fair probability comes only from a complete, two-way Pinnacle market on the
  same sport, event, market, period, line, team scope, and selection. Spreads
  must have opposite matching signed points; totals must have matching points.
  The app never falls back to soft-book consensus. Optional line-adjusted rows
  are disabled by default; when enabled, the scanner first interpolates between
  bracketing Pinnacle lines and otherwise uses a configured per-sport normal
  distribution model. It never extrapolates interpolation beyond Pinnacle's
  quoted range and always enforces the maximum line difference.
- Pinnacle vig is removed with the power method by default. The shared
  `scanner_config.py` config supports `power`, `shin`, or `multiplicative`,
  alongside the interactive board minimum EV (2%), 60-second quote-age limit,
  maximum Pinnacle overround (1.06), bankroll/Kelly caps, and line-adjusted
  controls. The automated worker uses a separate 3.5% floor (`worker_min_ev_pct`)
  and no upper cap (`worker_max_ev_pct = None`). Line
  adjustments default off, with `max_line_diff` set to 1.5 points and their
  Kelly fraction defaulting to half the exact-match fraction. The configured
  football model adds 4- and 2.5-percentage-point adjustments when spreads
  cross 3 and 7, respectively. If the feed has no explicit mainline marker,
  the model infers the mainline from the Pinnacle line whose fair probability
  is closest to 50% (ties use lower overround). Per-sport standard deviations
  are starting assumptions in config; validate them against line-adjusted CLV
  before enabling the feature.
- Quotes older than the configured age, markets over the overround limit, and
  already-started events are excluded. Rows with EV above 15% remain visible
  with a `verify` flag. Each scan reports fetched/kept counts, drop reasons,
  and the minimum, median, and maximum EV. Reported venue liquidity below the
  configured `min_liquidity_dollars` threshold is excluded; the default is
  `$25`, and quotes without a liquidity value are not filtered.
- For non-push lines, EV is `(decimal odds * fair probability - 1) * 100`.
  For whole-number spread/total lines, the model estimates win, lose, and push
  probabilities and uses `win_p * (decimal odds - 1) - lose_p`; a push returns
  the stake. The table shows those probabilities for each opportunity.
- Alt-line ladders are reduced to the highest-EV rung for each event, market
  type, period, team scope, and selection.
- Arbitrage chooses the best compatible price pair among selected venues in the
  same event, market, and period. It requires exactly two outcomes, different
  venues for the two legs, and opposite signed points that sum to zero on the
  same absolute spread line (for example Home +4.5 against Away -4.5). Alternate
  lines that quote the same sign for both teams are separate wagers, not
  opposite sides, and are never paired. Three-way moneylines (including a draw)
  are ignored even when the draw quote is missing, and totals must be the same
  line's Over and Under. NCAAB additionally requires main lines only, a $100
  liquidity floor, and excludes player props from arbitrage. Pinnacle is a
  reference only and is excluded from every arbitrage leg, even if a feed
  payload or manual input includes it. Player props match on event, market,
  player (ID, or normalized name when the ID is missing), and identical line;
  props without any player identity are ignored, selections must be Over and
  Under, and one-way milestone outcomes such as `30+` are ignored. Arbitrage is
  filtered by a $10.00 minimum guaranteed
  profit at the configured stake: after whole-dollar rounding, the opportunity
  is shown only when the minimum leg payout still clears the total stake by at
  least $10.00. The worker additionally keeps each leg at or below $300 and
  caps the total stake at $300.
  PropLine filters the API response with `api_max_age_seconds` (60s), while the
  scanner rejects locally stale quotes using `max_quote_age_seconds` (300s).
  The two are deliberately separate: PropLine's `last_update` values are
  typically 2-5 minutes old, so a 60s local window discarded ~97% of quotes
  and hid real opportunities, while raising the API-side window only increased
  payload size and memory. The UI snapshot does not auto-refresh; scan again
  before acting.
  Stake allocations use the selected whole-dollar rounding increment; the app
  recalculates and displays only opportunities whose rounded stakes still
  guarantee a positive return.
- Kelly uses `f = (fair probability * decimal odds - 1) / (decimal odds - 1)`,
  then applies the selected fractional-Kelly multiplier. Each suggestion is
  capped at 1% of bankroll per bet and 3% per event, and rounded down to cents.
  Kelly remains sensitive to probability errors and does not remove betting risk.
- Pinnacle-reference opportunities and eligible Pinnacle market snapshots are
  logged locally in `data/bets.db`. After an event starts, the app calculates
  CLV against the latest stored pre-kickoff Pinnacle fair probability for the
  same selection. Exact-match and line-adjusted rows are stored and displayed
  in separate CLV buckets; adjusted rows estimate the close at the venue's
  line from same-scan Pinnacle snapshots. This is an observed close estimate,
  not an official closing line; scan near kickoff for more useful CLV data.
- To track a wager you actually placed, use **Log** beside a +EV row or
  **Log placed arbitrage** on an arbitrage card, then confirm the actual odds
  and stake. Arbitrage legs are saved as separate tracker rows in one
  transaction; uncheck any leg you did not place. Grade each placed leg after
  the event to update net profit and ROI. These actions record bets only; they
  do not place wagers. Manual entry remains available for bets not shown by a
  scan.

The app cannot ensure a quote is still available or that every leg is accepted.
Arbitrage calculations exclude limits, rejected wagers, price movement, fees,
and settlement-rule differences. Exchange fees and bid/ask spreads are not
deducted from +EV calculations. Verify prices, fees, and market rules at each
venue before acting.

PropLine's odds feed supplies `price` as American odds. The app keeps that
source price and converts it once to decimal odds for EV and arbitrage math.
Where PropLine provides exchange liquidity and its update time, these are
retained with the quote. Liquidity does not cap the suggested arbitrage stake,
and exchange fees are not included. Underdog's source price is used as
provided; its payout multiplier is metadata and is not applied a second time.
