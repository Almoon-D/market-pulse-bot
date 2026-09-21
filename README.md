# mercados

Tracks the real-time operational status of global stock exchanges and
detects unscheduled market-wide closures, publishing three self-updating
messages to Discord, Slack, or Telegram:

1. **Upcoming Events & Holidays** — 5-business-day forecast, all regions.
2. **Live Dashboard — Americas & Europe**.
3. **Live Dashboard — Asia & Oceania**.

Licensed under the [GNU Affero General Public License v3.0](LICENSE).

---

## Requirements: Python 3.14, exclusively

This project targets **Python 3.14 and nothing earlier** — `pyproject.toml`
sets `requires-python = ">=3.14"`, and the code uses 3.14-native typing with
no `from __future__ import annotations` workaround. It will not run on 3.13
or earlier.

If your OS's default `python3` is older than 3.14, install 3.14 with one of:

- **pyenv** (any Linux/macOS):
  ```bash
  curl https://pyenv.run | bash
  pyenv install 3.14.0
  pyenv local 3.14.0
  ```
- **deadsnakes PPA** (Ubuntu/Debian):
  ```bash
  sudo add-apt-repository ppa:deadsnakes/ppa
  sudo apt update && sudo apt install python3.14 python3.14-venv
  ```
- **Official installer**: download from
  [python.org/downloads](https://www.python.org/downloads/) (macOS/Windows
  installers, or source tarball for Linux).

Verify before proceeding:

```bash
python3.14 --version   # Python 3.14.x
```

### Virtual environment & install

```bash
python3.14 -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -e .
```

This pulls in `pydantic>=2.12`, `pydantic-settings>=2.14`,
`exchange_calendars>=4.13.2`, `httpx`, and `PyYAML` — see `pyproject.toml`.
`pydantic.v1` is never imported anywhere in this codebase: the V1
compatibility shim is broken on 3.14 and fails silently, so it's simplest
to never go near it.

---

## Quick start

```bash
# 1. Scaffold the exchange roster. config/exchanges.yaml ships with the 4
#    fully-worked anchors (XNYS, XMAD, XTKS, XSPX); this adds the other 20.
python main.py init-config

# 2. Configure credentials for your chosen backend.
cp .env.example .env
$EDITOR .env

# 3. One-shot run (good for testing, or for driving from cron/a scheduler):
python main.py

# 4. Or run as a daemon that keeps itself updated:
python main.py --loop --interval 30 --incident-interval 90
```

---

## Configuration

### `config/exchanges.yaml`

One entry per monitored exchange. Two `calendar_type`s:

- **`exchange_calendars`** — backed by the
  [`exchange_calendars`](https://github.com/gerrymanoim/exchange_calendars)
  package. The `mic` field is validated at config-load time against that
  package's calendar registry — an unknown MIC fails immediately at
  startup, not three hours into a `--loop` deployment. Note the calendar
  MIC for Madrid is **`XMAD`**, not `BMEX`.
- **`synthetic`** — fully self-contained in YAML (`timezone`, `open_time`,
  `close_time`, optional `lunch_start`/`lunch_end`, `trading_days`,
  `holidays`), for exchanges `exchange_calendars` doesn't cover. The
  shipped anchor `XSPX` (Fiji, UTC+12, 10:00–12:00) is one example; two of
  the 20 scaffolded exchanges (Shenzhen `XSHE` and NSE India `XNSE`) are
  scaffolded as synthetic for the same reason — check
  `exchange_calendars.get_calendar_names()` yourself before assuming any
  exchange is covered.

Every exchange also carries `session_offsets` (minutes for pre-market,
opening auction, closing auction, post-market, relative to the official
open/close) and an `incident_source` (see below). Run
`python main.py init-config` to append the other 20 named exchanges
(TSX, B3, LSE, Xetra, Euronext Paris/Amsterdam/Milan, SIX, HKEX, SSE, SZSE,
BSE, NSE India, KRX, TWSE, SGX, ASX, Cboe Australia, Chi-X, NZX) with
`calendar_type`/`timezone`/`tz_label`/`currency`/`country_flag`/`region`
pre-filled — it's additive and idempotent, so it's safe to run again later
and it will only ever add what's missing, never touch what's already
there.

### `incident_source` and its three types

- `structured_feed` — a feed with reliably parseable per-entry fields
  (even if delivered as RSS/XML — NASDAQ's trade-halts feed is the
  anchor example, and it counts as "structured" because each item has
  well-defined fields, not because of the transport format).
- `rss_keyword` — last resort. Logs an "unconfirmed candidate" when a
  keyword matches; **never** autonomously sets 🔶/🟥/🚨.
- `manual` — the default/majority case, and the only channel most of the
  24 exchanges have. It's also a universal override: every exchange is
  checked against `data/manual_incidents.yaml` every incident-poll cycle
  regardless of its configured type, so an operator can always force an
  exchange-wide incident by hand — including on XNYS, whose own
  structured feed is `scope: single_stock` and therefore can never raise
  a market-wide flag automatically.

Only a `structured_feed` source with `scope: market_wide` is ever allowed
to autonomously set an incident phase. `data/manual_incidents.yaml` is
absent by default (meaning "no manual overrides"); its schema:

```yaml
incidents:
  - mic: "XNYS"
    phase: "regulatory_halt"   # regulatory_halt | technical_halt | exceptional_closure | post_halt_reopening
    note: "Level 1 circuit breaker triggered market-wide"
    reopening_time: null       # ISO 8601, set once officially announced
```

### `.env` / environment variables

See `.env.example`. Backend credentials, `display_timezone`, and
`language` (`es`/`en`/`de`/`fr`, default `es`) are loaded via
`pydantic-settings` with prefix `MERCADOS_`. Missing credentials for
whichever `notification_backend` you selected fail validation immediately
at startup with a specific, actionable message rather than an opaque
error the first time the app tries to publish.

---

## Why MIC codes never appear in output

`mic` is an internal key used to validate calendars, look up schedules,
and address exchanges in `manual_incidents.yaml` — it is deliberately
never interpolated into any rendered message. What a Discord/Slack/
Telegram reader sees is always the human `name`, `country_flag`, and
`currency` fields. This is enforced by construction: `text_formatter.py`
never receives or touches the `mic` field at all.

---

## The three-message architecture, and why every backend shares one formatter

`src/text_formatter.py` builds all three payloads from the same phase-state
data with the same wording, then packages that content differently per
backend (a Discord embed with fields, a Slack Block Kit body, or a plain
Telegram string) — this is deliberate: the whole point of Section 5's
`NotificationBackend` abstraction is that backends differ *only* in HTTP
mechanics, never in what's said. If Discord and Telegram ever showed
different wording for the same market state, that would be a formatter
bug, not a backend-specific feature.

Messages 2 and 3 are split by region pair (Americas+Europe /
Asia+Oceania) rather than sent as one combined dashboard because a single
message listing all four regions would risk exceeding Telegram's hard
4,096-character ceiling (which rejects the request outright — no
truncation grace) as the roster grows past a handful of exchanges, and
because the two halves are useful to an operator at very different times
of day.

---

## Time rendering

No timezone API calls, ever — everything is computed locally via the
standard-library `zoneinfo`. Every rendered time follows the same order:

1. countdown + absolute clock time in `display_timezone`, then
2. the exchange's own local time in parentheses, labeled with `tz_label`.

```
⚫️🔜🟣 🇺🇸 NYSE (USD) — Pre-market en 8 minutos, a las 08:30 (04:30 ET)
```

Times are rendered zero-padded 24-hour (`08:30`, not `8:30`) for
unambiguous, locale-agnostic display. Countdown minutes are computed by
flooring `(target − now) / 60`; because every tick recomputes the current
phase from scratch against the live clock (there is no incrementally
updated countdown anywhere in `market_engine.py`), a transition time is
never stale by more than a rounding hair — the defensive zero-floor in
`_minutes_until` is insurance, not a normal code path.

---

## `data/state.json` — the three-slot schema

```json
{
  "backend": "discord",
  "events_message_ref": { "backend": "discord", "external_id": "123...", "channel": null },
  "dashboard_americas_eu_ref": { "backend": "discord", "external_id": "124...", "channel": null },
  "dashboard_asia_oceania_ref": { "backend": "discord", "external_id": "125...", "channel": null }
}
```

Written atomically (temp file + `rename`, which is atomic on both POSIX
and Windows) after every publish/update, so a crash mid-write can never
leave a corrupt or half-written state file. If you switch
`notification_backend` between runs, the old refs (pointing at messages on
a different platform) are discarded automatically and all three messages
are republished fresh — you'll see a warning logged when this happens.

---

## Concurrency model: one event loop, two clocks

Everything runs on a single `asyncio` event loop — no threads anywhere.
Two independent timers share it:

- **The render loop** (`main.py`, default 30s / `--interval`): computes
  each exchange's phase deterministically from its calendar + the shared
  incident table, renders the three static payloads, and is the *only*
  code that ever talks to the notification backend.
- **The incident-detection background task** (`src/halt_detector.py`,
  default 90s / `--incident-interval`, minimum 60s enforced by the CLI):
  polls each exchange's `incident_source` plus the manual-override file,
  and writes results into an `asyncio.Lock`-guarded `IncidentStore` — it
  never touches the network on the notification side, and the render loop
  never touches an exchange's incident feed directly.

In `--loop` mode both run for the process's lifetime, coordinated by a
shared `asyncio.Event` for clean shutdown on `SIGINT`/`SIGTERM`. In
one-shot mode (see below) there's no background task at all — a single
invocation runs one incident-detection pass inline, then one render/
publish pass, then exits; the cadence is entirely up to whatever external
scheduler is invoking it.

---

## Deployment

Two paths, in `deploy/`:

- **`deploy/mercados.service`** — a systemd unit running `--loop` as a
  persistent daemon. Points `ExecStart` at an explicit
  `.../bin/python3.14` path rather than a bare `python3` that could
  silently resolve to an older interpreter after an OS update, and uses
  `StateDirectory=` so `data/` survives restarts.
- **`deploy/mercados-oneshot.cron`** — driving one-shot mode from cron
  instead. Labeled **best-effort, unsuitable for sub-5-minute windows**:
  cron's minimum granularity is a minute, it doesn't guarantee your job
  starts exactly on the minute, and the notation system's shortest
  pre-notice threshold is 2 minutes (Section 1.B) — a scheduler with
  multi-second jitter eats directly into that margin. Use the systemd
  daemon if you need reliable sub-minute freshness.

---

## CLI reference

```
python main.py [run|init-config] [--loop] [--interval SECONDS] [--incident-interval SECONDS]
```

| Command / flag | Default | Notes |
|---|---|---|
| `run` (default) | — | One-shot unless `--loop` is given |
| `init-config` | — | Additively scaffold the 20 non-anchor exchanges |
| `--loop` | off | Daemon mode |
| `--interval` | 30 | Render loop cadence, seconds (`--loop` only) |
| `--incident-interval` | 90 | Incident-poll cadence, seconds; CLI enforces a 60s floor |

---

## Internationalization

`config.language` selects `es` (default) / `en` / `de` / `fr`. Every
user-facing string lives in `locales/{lang}.yaml`; `src/i18n.py` falls
back to `en` and logs a warning on any missing key rather than crashing a
live loop over one untranslated string.

---

## Known limitations

- `SyntheticCalendarConfig` has no holiday-calendar equivalent beyond an
  explicit `holidays:` list you maintain yourself — reasonable for a
  handful of synthetic exchanges, not a substitute for a real holiday
  calendar at scale.
- The `structured_feed` parser (`src/halt_detector.py`) ships a field-name
  mapping (`DEFAULT_STRUCTURED_FIELD_MAP`) matching a NASDAQ-trade-halts-
  shaped feed. Real feed schemas vary by exchange; treat this as the
  integration point to adapt, not a universal parser.
- `rss_keyword` matching is a plain case-insensitive substring check
  against a small English-centric default keyword list — sufficient to
  flag "review this by hand," not to drive automated decisions.
