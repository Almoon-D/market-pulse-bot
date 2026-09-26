# Market Pulse Bot

**Know exactly what every major stock exchange is doing, right now, without refreshing a single tab.**

Market Pulse Bot watches 26 exchanges across the Americas, Europe, Asia, and Oceania and keeps three messages — a 5-day holiday forecast and two live regional dashboards — permanently up to date in your Discord, Slack, or Telegram. Pre-market, opening auctions, continuous trading, lunch breaks, closing auctions, post-market, holidays, and exceptional halts — all tracked automatically, all shown in the same badge language everywhere you look.

Built for traders who need to know when a market opens without doing timezone math, for desks that want one shared source of truth instead of eleven browser tabs, and for anyone who has ever missed a closing auction because they were watching the wrong clock.

```
⚫️🔜🟣 🇺🇸 NYSE (USD) — Pre-market in 8 minutes, at 08:30 (04:30 ET)
🟢 🇪🇸 Bolsa de Madrid (EUR) — Regular session (14:32 CEST)
🔷 🇯🇵 Tokyo Stock Exchange (JPY) — Reopening underway — ≥5 min, possible extension
```

Available in English, Spanish, German, and French. Free, open-source, self-hosted — your data never leaves your own server.

---

## Phase & badge legend

Every exchange, in every language, uses exactly these badges — nothing more, nothing regional. Run `market-pulse-bot send-legend` any time to post this table (with real translations) to your own channel; it's never sent automatically.

| Badge | Phase | What it means |
|---|---|---|
| ⚫️ | Closed | No trading activity — it's night or the weekend at this exchange. |
| 🟣 | Pre-market / Post-market | Trading is possible but liquidity is reduced. |
| 🔵 | Opening / Closing auction | Orders are collected and matched at a single price, not continuously. |
| 🟢 | Regular session | Continuous trading is active. |
| 🔘 | Lunch break | Scheduled midday trading pause. |
| ⚪️ | Holiday | Official market holiday — closed for the full day. |
| 🌗 | Half day | Modifier shown alongside another badge on an early-close day. |
| 🔶 | Regulatory halt | Full-exchange regulatory or compliance halt. |
| ♦️ | Technical halt | Full-exchange technical or operational halt. |
| 🚨 | Exceptional closure | Circuit breaker, disaster, or regulator order — never a single-stock event. |
| 🔷 | Post-halt reopening | Reopening auction after a halt — duration can vary. |
| 🔜 | *(between two badges)* | A scheduled transition is coming up soon. |

---

## Global coverage

26 exchanges, four world regions. "Structure tracked" lists which real, source-verified microstructure phases are modeled beyond plain continuous trading. "Incident coverage" is deliberately blunt: **almost every venue here relies on an operator's manual word**, because no trustworthy, free, market-wide incident feed exists for most of the world's exchanges. Where a NASDAQ feed is wired in for Nasdaq itself, it only ever reports single-stock trading halts — it cannot and does not raise an exchange-wide alert by itself.

### Americas

| Exchange | Market | Structure tracked | Incident coverage |
|---|---|---|---|
| 🇺🇸 NYSE (`XNYS`) | The world's largest exchange by market cap, home to most S&P 500 blue chips. | Pre-market, closing auction, post-market | Manual |
| 🇺🇸 Nasdaq Stock Market (`XNAS`) | Separate exchange; `exchange_calendars` currently aliases XNAS to XNYS, and will use a dedicated XNAS calendar if a future compatible release provides one. | Pre-market, closing auction, post-market | NASDAQ feed (single-stock halts only, logged) + manual override |
| 🇲🇽 Mexican Stock Exchange (`XMEX`) | Mexico's primary equities exchange. | Native calendar; no unverified extended phases configured | Manual |
| 🇨🇱 Santiago Stock Exchange (`XSGO`) | Chile's primary equities exchange. | Native calendar; no unverified extended phases configured | Manual |
| 🇨🇦 Toronto Stock Exchange (`XTSE`) | Canada's primary exchange, heavy in energy and mining names. | Pre-market, closing call | Manual |
| 🇧🇷 B3 (`BVMF`) | Brazil's exchange; hours shift twice a year to stay aligned with US markets. | Pre-market, closing call | Manual |

### Europe

| Exchange | Market | Structure tracked | Incident coverage |
|---|---|---|---|
| 🇪🇸 Bolsa de Madrid (`XMAD`) | Spain's primary market, home to the IBEX 35. | Opening auction, closing auction (runs *after* the reported close), post-market | Manual |
| 🇬🇧 London Stock Exchange (`XLON`) | One of the world's oldest exchanges; home to the FTSE 100. | Opening auction, closing auction, post-market crossing session | Manual |
| 🇩🇪 Deutsche Börse Xetra (`XETR`) | Germany's electronic market, home to the DAX. | Opening auction, closing auction | Manual |
| 🇫🇷 Euronext Paris (`XPAR`) | France's market and Euronext's largest venue; a genuinely long ~1h45m opening call. | Opening auction, closing auction, Trading At Last | Manual |
| 🇳🇱 Euronext Amsterdam (`XAMS`) | The world's oldest stock exchange, now part of the pan-European Euronext group. | Opening auction, closing auction, Trading At Last | Manual |
| 🇮🇹 Euronext Milan (`XMIL`) | Italy's exchange, home to the FTSE MIB, joined Euronext in 2021. | Opening auction, closing auction, Trading At Last | Manual |
| 🇨🇭 SIX Swiss Exchange (`XSWX`) | Switzerland's primary exchange. | Closing auction, Trading-At-Last window | Manual |

### Asia

| Exchange | Market | Structure tracked | Incident coverage |
|---|---|---|---|
| 🇯🇵 Tokyo Stock Exchange (`XTKS`) | Asia's largest exchange by market cap; native midday lunch break. | Opening call, closing call, lunch break | Manual |
| 🇭🇰 Hong Kong Exchange (`XHKG`) | Asia's key gateway market, native midday lunch break. | Pre-open session, closing auction, lunch break | Manual |
| 🇨🇳 Shanghai Stock Exchange (`XSHG`) | Mainland China's larger exchange by market cap. | Opening call, closing call, lunch break | Manual |
| 🇨🇳 Shenzhen Stock Exchange (`XSHE`) | Mainland China's tech- and growth-heavy exchange. | Opening call, closing call, lunch break | Manual |
| 🇮🇳 BSE India (`XBOM`) | Asia's oldest stock exchange, founded 1875. | Pre-open session, post-close settlement window | Manual |
| 🇮🇳 National Stock Exchange of India (`XNSE`) | India's largest exchange by trading volume. | Pre-open session, post-close settlement window | Manual |
| 🇰🇷 Korea Exchange (`XKRX`) | South Korea's exchange, home to the KOSPI. | Pre-market call, closing call | Manual |
| 🇹🇼 Taiwan Stock Exchange (`XTAI`) | Taiwan's primary market. | Closing call only — no reliable pre-market data found, left unmodeled | Manual |
| 🇸🇬 Singapore Exchange (`XSES`) | Southeast Asia's key financial hub. | Opening routine, closing routine, Trade-at-Close† | Manual |

† SGX has a real, official midday lunch break that this release does not yet detect — see [Known limitations](#known-limitations).

### Oceania

| Exchange | Market | Structure tracked | Incident coverage |
|---|---|---|---|
| 🇫🇯 South Pacific Stock Exchange (`XSPX`) | Fiji's small, synthetic-calendar market. | Simple continuous session — genuinely has no auction structure | Manual |
| 🇦🇺 Australian Securities Exchange (`XASX`) | Australia's primary exchange. | Pre-open, opening/closing single-price auctions, post-close | Manual |
| 🇦🇺 Cboe Australia (`CHIA`) | A second, competing order book for the same ASX-listed stocks. | Continuous only — Cboe itself confirms no market-wide auction for the securities it trades | Manual |
| 🇳🇿 New Zealand Exchange (`XNZE`) | New Zealand's primary exchange. | Pre-open, closing auction (after the reported close) | Manual |

---

## Getting started

```bash
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"

cp .env.example .env
# Edit .env: pick discord, slack, or telegram, and add its credentials.

python main.py init-config   # normalizes config/exchanges.yaml (already ships all 26)
python main.py               # one-shot: publish/update the three messages once
```

For continuous operation:

```bash
python main.py --loop --interval 30 --incident-interval 90
```

The systemd unit in `deploy/market-pulse-bot.service` is the recommended way to run this in production — see [Deployment](#deployment).

### Post the phase legend

```bash
python main.py send-legend
```

Posts the phase/badge legend table (translated into whichever `language` your `.env` selects) to your configured channel, once, on request — it is never sent as part of the regular updates. On Telegram and Slack the bot will also try to pin it if it has the right permissions/scope; on Discord it publishes normally but can't pin, because Discord's webhook-only design (deliberately simple, no bot login required) has no access to the pin endpoint at all — that's a platform-level limitation, not a bug to report.

---

## How it decides what to show you

- **Scheduled state** — pre-market, auctions, regular trading, lunch, post-market, holidays — is computed locally and deterministically from each exchange's real calendar plus config in `config/exchanges.yaml`. No paid market-hours API, no rate limits, no external dependency that can go down at the worst moment.
- **Incidents** — regulatory halts, technical halts, exceptional closures — are conservative by design. A single-stock trading halt can never be mistaken for a market-wide event; only an operator's explicit manual entry, or a feed you've specifically configured and verified as market-wide, can raise one.
- **One shared vocabulary.** Discord, Slack, and Telegram all show the same badges and the same wording — the only difference is how each platform's API wants the message packaged.

---

## Configuration

Full reference details — the `phase_windows` schema, the `incident_source` YAML format, the `data/state.json` three-slot structure, synthetic-calendar definitions, and every CLI flag — are documented inline in `config/exchanges.yaml`, `.env.example`, and each module's docstring. The short version:

```bash
cp .env.example .env   # pick a backend, add credentials, pick a language
python main.py init-config   # scaffold/normalize the exchange roster
```

Manual incident overrides live in `data/manual_incidents.yaml` (not committed):

```yaml
incidents:
  - mic: XNYS
    phase: technical_halt
    note: "Market-wide operational interruption"
    reopening_time: null   # ISO-8601 UTC once officially known
```

---

## Known limitations

- **Singapore's midday break isn't detected yet.** SGX has a real, official market-wide lunch break (its own Rulebook confirms 12:00–13:00), but the underlying calendar library doesn't expose it for this venue, and phase-window config can't add it (lunch detection is intentionally separate from the auction/pre-post-market system). Tracked as a known gap, not silently worked around.
- **Two venues genuinely have no auction structure modeled**, and that's correct, not incomplete: Fiji's SPX has none in reality, and the vast majority of what trades on Cboe Australia has none either (confirmed against Cboe's own FAQ) — adding a fabricated auction window to either would make the data *less* accurate, not more.
- **Taiwan's pre-market phase isn't modeled** — no reliably-sourced timetable for it was found, so it was left out rather than guessed at.
- **Most incident coverage is manual by design**, not by oversight. See [How it decides what to show you](#how-it-decides-what-to-show-you).
- **Scaffold-tier exchanges** (everything except the four original anchors — NYSE, Madrid, Tokyo, and Fiji) have real, source-checked operational hours as of this release, but holiday-calendar coverage for the synthetic ones (Shenzhen, NSE India, Cboe Australia) still depends on data you maintain yourself.

---

## Repository layout

```text
market-pulse-bot/
├── .env.example
├── .gitignore
├── LICENSE
├── README.md
├── pyproject.toml
├── main.py
├── config/exchanges.yaml
├── data/.gitkeep
├── deploy/
│   ├── market-pulse-bot.service
│   └── scheduler-example.sh
├── locales/{en,es,de,fr}.yaml
├── src/market_pulse_bot/
│   ├── app.py
│   ├── calendar_engine.py
│   ├── config.py
│   ├── halt_detector.py
│   ├── i18n.py
│   ├── market_engine.py
│   ├── notification_backend.py
│   └── text_formatter.py
├── tests/
└── .github/workflows/ci.yml
```

## Validation

CI runs, on every change, against Python 3.14: compile check, `pytest`, `ruff`, `mypy` (strict), a targeted credential scan, and a permanent grep guard against the retired red-square glyph ever reappearing.

```bash
python -m compileall -q main.py src tests
python -m pytest -q
python -m ruff check .
python -m mypy src
```

## Deployment

```bash
sudo cp deploy/market-pulse-bot.service /etc/systemd/system/market-pulse-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now market-pulse-bot
```

This runs `main.py --loop --interval 30 --incident-interval 90` continuously and is the recommended mode for reliable 30-second updates. `deploy/scheduler-example.sh` is available for cron-driven one-shot mode instead, but is explicitly best-effort — see the comments in that file for why sub-5-minute cron scheduling isn't reliable enough for this system's shortest pre-notice windows.

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE). No credentials or runtime state should ever be committed to Git; see `.gitignore`.
