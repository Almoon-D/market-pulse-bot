# Market Pulse Bot

Deterministic stock-exchange market-status monitor for Python 3.14+, publishing three continuously updated messages to Discord, Slack, or Telegram.

The service is deliberately conservative about incidents: scheduled state is calculated locally from exchange calendars and configuration, manual overrides always win, and editorial RSS keyword matches can never raise an exchange-wide incident by themselves.

## What it does

The service publishes exactly three persistent messages:

1. **Upcoming Events & Holidays**: the next five business days across all monitored regions.
2. **Live Dashboard: Americas & Europe**.
3. **Live Dashboard: Asia & Oceania**.

The process uses one asyncio event loop with two independent clocks:

- **Render/publish loop**: 30 seconds by default, configurable with `--interval`.
- **Incident detector**: 90 seconds by default, minimum 60 seconds, configurable with `--incident-interval`.

Only the render loop is allowed to call Discord, Slack, or Telegram.

## Requirements

- Python **3.14+**
- `pydantic>=2.12`
- `pydantic-settings>=2.14`
- `exchange_calendars>=4.13.2`
- `httpx>=0.28`
- `PyYAML>=6.0`

The application uses the standard-library `zoneinfo` for timezone handling and does not call a market-hours API.

## Quick start

```bash
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"

cp .env.example .env
# Edit .env with one notification backend.

python main.py init-config
python main.py
```

For continuous operation:

```bash
python main.py --loop --interval 30 --incident-interval 90
```

The systemd service in `deploy/market-pulse-bot.service` is the recommended production mode.

## Configuration

### Exchange roster

The canonical configuration is:

```text
config/exchanges.yaml
```

The repository contains **24 unique MICs**. Four are the fully worked anchors:

- `XNYS` — NYSE, `exchange_calendars`
- `XMAD` — Bolsa de Madrid, `exchange_calendars`
- `XTKS` — Tokyo Stock Exchange, `exchange_calendars`
- `XSPX` — South Pacific Stock Exchange, synthetic

Other exchanges are intentionally conservative scaffolds. Their calendar/timezone metadata is populated, but holiday treatment and operational windows should be independently reviewed before being treated as authoritative.

`init-config` normalizes the checked-in roster and is idempotent. It does not silently invent new exchange definitions at runtime.

### Session phases

Operational windows are represented explicitly as `phase_windows` rather than assuming that every exchange has the same auction topology.

A window is anchored to either:

- `session_open`
- `session_close`

and has signed minute offsets:

```yaml
phase_windows:
  - phase: opening_auction
    anchor: session_open
    start_offset_minutes: -30
    end_offset_minutes: 0
```

This supports exchanges whose closing auction starts at the official close, as well as exchanges with a pre-close auction.

A zero-duration auction is not forced into a fake interval. For venues where the auction itself is an instantaneous uncrossing event, the dashboard transitions directly to the next observable phase.

### Time display

Every transition is rendered with:

```text
[current]🔜[target]
```

Absolute times are shown in the configured display timezone, followed by the exchange-native time and `tz_label`. All countdowns are recomputed from the live UTC clock on every render tick and are never allowed to become negative.

MIC codes are internal identifiers and are never rendered.

## Incident safety

Each exchange has an `incident_source`:

```yaml
incident_source:
  type: manual | structured_feed | rss_keyword
  scope: market_wide | single_stock
```

Rules are intentionally strict:

- A `structured_feed` may autonomously set an incident only when its configured scope is `market_wide`.
- A `single_stock` source can never change an exchange-wide state.
- `rss_keyword` only creates an unconfirmed candidate in logs.
- `manual` is the operator override and always has priority.

The manual override file is not committed:

```text
data/manual_incidents.yaml
```

Example:

```yaml
incidents:
  - mic: XNYS
    phase: technical_halt
    note: "Market-wide operational interruption"
    reopening_time: null
```

Use an ISO-8601 UTC timestamp when `reopening_time` is known.

Incident detection is deliberately **not** marketed as universal real-time coverage. Most exchanges currently use manual incident sources until a trustworthy market-wide feed is explicitly configured and tested.

## Notification backends

### Discord

Uses the Discord webhook REST API.

Initial message creation uses `?wait=true` so the created message ID is returned and can be persisted. Existing messages are edited in place.

The renderer validates Discord embed field, component, and total-character limits before sending.

### Slack

Requires a bot token with `chat:write` and a target channel. Messages are created with `chat.postMessage` and updated with `chat.update`.

Incoming webhooks alone are insufficient because this service must edit the persistent dashboard messages.

### Telegram

Uses `sendMessage` and `editMessageText`. The normal Telegram “message is not modified” response is treated as an idempotent success.

## Persistent message state

Runtime state lives in:

```text
data/state.json
```

It stores one reference for each of the three messages.

References are typed by backend:

- Discord: message ID
- Slack: channel ID + timestamp
- Telegram: chat ID + message ID

State is written atomically with a temporary file, flush, `fsync`, and `os.replace`.

If the stored backend changes, all old references are discarded and the three messages are recreated.

If one message disappears, only that slot is recreated. The other two references are untouched.

Corrupt state is quarantined and the service starts with an empty state rather than failing permanently.

## Scheduling and calendars

`exchange_calendars` supplies exchange sessions, official opens/closes, native breaks, and early-close information where the selected calendar exposes them.

Synthetic exchanges are defined entirely in YAML with:

```yaml
synthetic:
  open_time: "10:00"
  close_time: "12:00"
  lunch_start: null
  lunch_end: null
  trading_days: [0, 1, 2, 3, 4]
  holidays: []
```

The calendar abstraction keeps scheduled state independent of notification backends and network incident feeds.

## Repository layout

```text
market-pulse-bot/
├── .env.example
├── .gitignore
├── LICENSE
├── README.md
├── pyproject.toml
├── main.py
├── config/
│   └── exchanges.yaml
├── data/
│   └── .gitkeep
├── deploy/
│   ├── market-pulse-bot.service
│   └── scheduler-example.sh
├── locales/
│   ├── es.yaml
│   ├── en.yaml
│   ├── de.yaml
│   └── fr.yaml
├── src/
│   └── market_pulse_bot/
│       ├── app.py
│       ├── calendar_engine.py
│       ├── config.py
│       ├── halt_detector.py
│       ├── i18n.py
│       ├── market_engine.py
│       ├── notification_backend.py
│       └── text_formatter.py
├── tests/
│   ├── test_config.py
│   ├── test_formatter.py
│   ├── test_incidents.py
│   ├── test_market_engine.py
│   └── test_notification_state.py
└── .github/
    └── workflows/
        └── ci.yml
```

## Validation

CI runs:

```text
Python 3.14
syntax/compile check
pytest
ruff
mypy
targeted credential scan
forbidden-glyph scan
```

The test suite covers the four anchor exchanges, transition-window behavior, manual incident precedence, RSS safety, per-slot message recovery, state backend mismatch, and formatting constraints.

For local development:

```bash
python -m compileall -q main.py src tests
python -m pytest -q
python -m ruff check .
python -m mypy src
```

## Operational limitations

The important limitation is incident coverage, not scheduled-calendar calculation. A reliable exchange-wide incident feed must be explicitly verified per venue before it is allowed to alter operational state.

Synthetic calendars also require maintained holiday data. A synthetically defined opening/closing schedule is not a substitute for a complete official exchange holiday calendar.

The current repository therefore distinguishes between a solid application architecture and exchange-specific operational data that still needs venue-level verification.

## Deployment

For production, use the systemd service:

```bash
sudo cp deploy/market-pulse-bot.service /etc/systemd/system/market-pulse-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now market-pulse-bot
```

The service runs:

```text
main.py --loop --interval 30 --incident-interval 90
```

One-shot execution remains useful for manual checks and external schedulers.

## License

GNU Affero General Public License v3.0. See [LICENSE](LICENSE).

No credentials or runtime state should be committed to Git.
