# Global Market Operations Monitor

Production-oriented Python monitor for the operational state of global stock exchanges. It computes market phases locally from exchange calendars or fully defined synthetic schedules, detects qualifying market-wide incidents asynchronously, and maintains three self-updating notification messages through Discord, Slack, or Telegram.

## Runtime

**Python 3.14 is the sole supported runtime.** The project deliberately does not support Python 3.13 or older, and does not depend on Python's optional free-threaded/no-GIL build.

Required runtime dependencies include:

- `pydantic>=2.12`
- `pydantic-settings>=2.14`
- `exchange_calendars>=4.13.2`
- `httpx`
- `PyYAML`

### Installing Python 3.14

If the operating system ships an older Python, install 3.14 explicitly rather than changing the project to accommodate the older interpreter.

#### pyenv

```bash
pyenv install 3.14.0
pyenv local 3.14.0
python --version
```

Use the current released 3.14.x patch version available to your platform if it differs from the example.

#### Ubuntu / Debian

Use a repository that provides Python 3.14, or install the current 3.14 package set through the distribution's supported Python packaging path. Do not point this application at the system `python3` when it resolves to an older release.

#### Official installer

Use the official Python 3.14 installer for your operating system and ensure `python` resolves to Python 3.14 inside the project environment.

## Installation

From the project root:

```bash
python -m venv .venv
```

Activate it:

```bash
# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Then install the project:

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

Verify:

```bash
python --version
python main.py --help
```

## Configuration

The repository ships four fully worked exchange anchors in `config/exchanges.yaml`:

- `XNYS` NYSE
- `XMAD` Madrid Stock Exchange, using the calendar MIC `XMAD`, not `BMEX`
- `XTKS` Tokyo Stock Exchange, including its lunch break and manual incident source
- `XSPX` South Pacific Stock Exchange synthetic schedule, Fiji UTC+12, 10:00–12:00, manual incidents

The other twenty markets are scaffolded by `init-config` with their `calendar_type`, timezone, timezone label, ISO 4217 currency, country flag, and region already populated. Their operational offsets and incident sources start conservatively and can be adjusted in YAML after confirming the venue's exact rules.

Run:

```bash
python main.py init-config
```

This creates:

- `config/exchanges.yaml` with the four anchors plus the twenty scaffolds.
- `config/config.yaml` with application settings and notification credentials.

`init-config` is idempotent with respect to MICs already present in `config/exchanges.yaml`.

### Application YAML

Example structure:

```yaml
language: "es"
display_timezone: "Europe/Madrid"
notification_backend: "discord"
credentials:
  discord_webhook_url: "https://discord.com/api/webhooks/..."
  slack_bot_token: null
  slack_channel_id: null
  telegram_bot_token: null
  telegram_chat_id: null
loop_interval: 30
incident_interval: 90
notification_thresholds:
  opening_auction: 10
  regular_after_auction: 2
  lunch_break: 5
  lunch_reopening: 5
  closing_auction: 5
  extended_hours: 15
  closed: 5
exchanges:
  # copy/edit entries from config/exchanges.yaml
```

Supported languages are `es`, `en`, `de`, and `fr`. `display_timezone` accepts an IANA timezone string and defaults to `Europe/Madrid`.

### Exchange configuration

Each exchange declares:

- `calendar_type`: `exchange_calendars` or `synthetic`.
- `timezone` and `tz_label`.
- `currency` as ISO 4217.
- `country_flag` and `region`.
- `session_offsets`: pre-market, opening auction, closing auction, and post-market minutes.
- Optional `lunch` times.
- A typed `incident_source`.

`exchange_calendars` entries are validated against the library's calendar registry at startup. A synthetic exchange must define its timezone, open, close, and trading days entirely in YAML. This is the path for venues not supported by `exchange_calendars`.

MIC codes are **internal identifiers only**. They never appear in rendered messages. Operators see the exchange's configured human name, flag, and currency instead.

## Three-message architecture

The application maintains exactly three independent messages:

1. **Upcoming Events & Holidays**: a five-business-day forecast across all configured regions.
2. **Live Dashboard — Americas & Europe**.
3. **Live Dashboard — Asia & Oceania**.

The split is intentional. Telegram rejects oversized messages at its 4,096-character limit; Discord has a 6,000-character embed budget plus per-field limits; Slack's update/block limits are similarly restrictive. The formatter therefore constructs all three messages before any notification request is made and validates them against the selected backend's character budget.

All backends consume the same static text payloads. The backend layer only handles HTTP mechanics and message references. This prevents Discord, Slack, and Telegram from drifting into three different rendering implementations.

### Static time rendering

No external time API is used. `zoneinfo` performs local timezone conversion. Every rendered transition contains:

1. the countdown/absolute time in `display_timezone`;
2. the exchange's native local time with its configured timezone label.

The text is regenerated each loop interval. Platform-native dynamic timestamp syntax is deliberately not used.

## Incident detection and concurrency

The application uses one `asyncio` event loop, not threads.

There are two logical clocks:

- **Main rendering clock**: default 30 seconds. It computes session phase locally and is the only task allowed to call the notification backend.
- **Incident clock**: default 90 seconds and minimum 60 seconds. A background asyncio task polls configured incident sources and writes only incident state into an `asyncio.Lock`-guarded in-memory store.

Incident priority is strict:

1. `structured_feed`
2. `rss_keyword`, which records an unconfirmed candidate and never autonomously creates a regulatory/technical/exceptional incident
3. `manual`

Only a `structured_feed` with `scope: market_wide` may autonomously create an incident override. A single-stock halt therefore cannot become a market-wide closure.

## Message state persistence

`data/state.json` is written atomically using a temporary file followed by rename. The schema contains one backend tag and exactly three named message slots:

```json
{
  "backend": "discord",
  "events_message_ref": {
    "backend": "discord",
    "message_id": "123456789"
  },
  "dashboard_americas_eu_ref": {
    "backend": "discord",
    "message_id": "123456790"
  },
  "dashboard_asia_oceania_ref": {
    "backend": "discord",
    "message_id": "123456791"
  }
}
```

Slack references additionally retain the channel ID. Telegram references retain the chat ID.

If a stored message disappears, the backend self-heals that slot only and recreates the missing message. The other two message references are preserved.

## Discord: `?wait=true` is mandatory

Every first-run Discord webhook POST appends `?wait=true`. Without that parameter Discord may return `204 No Content` with no message object, leaving the application without the message ID required for future PATCH updates.

Discord updates use the webhook message endpoint directly. HTTP 429 responses use exponential backoff and respect `Retry-After` when supplied.

## Running

### One-shot mode

The default mode performs one incident poll, computes the current state, and synchronizes all three messages:

```bash
python main.py
```

### Daemon mode

```bash
python main.py --loop
```

The default main interval is 30 seconds and the default incident interval is 90 seconds.

Override either interval without editing YAML:

```bash
python main.py --loop --interval 30 --incident-interval 90
```

The incident interval is constrained to a minimum of 60 seconds by the Pydantic configuration model.

Use a different config file when required:

```bash
python main.py --config /etc/market-monitor/config.yaml --loop
```

## Deployment

### systemd

`deploy/market-monitor.service` is the persistent-service template. It deliberately references a Python 3.14 interpreter explicitly in `ExecStart`, rather than trusting an older OS `python3` symlink. Set the service's `WorkingDirectory`, user, and virtual-environment path for the host before enabling it.

Typical flow:

```bash
sudo cp deploy/market-monitor.service /etc/systemd/system/market-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now market-monitor
sudo systemctl status market-monitor
```

The service keeps `data/state.json` under the project directory so message references survive process restarts.

### External scheduler

`deploy/scheduler-example.sh` is a best-effort scheduler example. It is **unsuitable for sub-five-minute windows** because scheduler jitter, process startup, and network latency make precise high-frequency refreshes unreliable. Use `--loop` under a persistent service when the operational cadence matters.

## Operational design notes

- No notification backend performs market-state calculations.
- No notification backend owns a second formatter.
- No MIC code is exposed to operators.
- Calendar computation and session-phase computation are offline and deterministic once the calendar library has been loaded.
- Network I/O is isolated to incident polling and notification delivery.
- The application does not place trades or send broker orders.

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0). See `LICENSE`.
