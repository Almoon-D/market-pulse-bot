#!/usr/bin/env sh
# Best-effort, cron-driven one-shot mode: each call publishes/updates the
# four messages once and exits. Cron can't run more often than once a
# minute and gives no timing guarantees, so the shortest pre-notice
# windows (2-5 minutes before a transition) are easily missed, and the
# countdown badges only appear in --loop mode anyway. Prefer the systemd
# unit (deploy/market-pulse-bot.service), which runs --loop continuously.
set -eu
cd /opt/market-pulse-bot
exec .venv/bin/python3.14 main.py
