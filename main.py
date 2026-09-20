"""Poll loop wiring everything together."""

import datetime as dt
import time

from .calendars import build_engine
from .config import load_settings
from .detector import compute_phase
from .events import collect_upcoming_events
from .i18n import translate
from .incidents import check_incident, clear_manual, escalate_manual
from .logging import get_logger
from .notifiers import build_backend, guarded_send
from .persistence import load_state, save_state
from .render import render_dashboard, render_phase_change
from .state import ExchangeState, Phase

logger = get_logger()

DASHBOARD_BUSINESS_DAYS = 5
DASHBOARD_INTERVAL_SECONDS = 900  # every 15 min


def run(config_path: str) -> None:
    settings = load_settings(config_path)
    language = settings.language
    backend = build_backend(settings)
    logger.info(translate(language, "info.startup"))

    engines: dict[str, object] = {}
    states: dict[str, ExchangeState] = {}
    for cfg in settings.exchanges:
        engines[cfg.mic] = build_engine(cfg)
        st = ExchangeState(cfg)
        load_state(settings.data_dir, st)
        states[cfg.mic] = st

    last_dashboard = 0.0

    while True:
        now = dt.datetime.now(dt.timezone.utc)

        for cfg in settings.exchanges:
            state = states[cfg.mic]
            engine = engines[cfg.mic]

            # incident detection overrides the schedule
            incident = check_incident(cfg, state, language)
            phase, next_dt, _next_phase = compute_phase(cfg, engine, state, now)
            if incident is not None and phase not in (Phase.HOLIDAY,):
                phase = incident
                if state.exceptional_since is None:
                    escalate_manual(state, now)
            elif state.exceptional_since is not None and incident is None:
                clear_manual(state)
                state.set_phase(Phase.CLOSED, now)

            if state.set_phase(phase, now):
                guarded_send(
                    backend,
                    render_phase_change(cfg, state.prev_phase, phase, next_dt, now, language),
                    language,
                )
            state.next_transition = next_dt
            save_state(settings.data_dir, state)

        if time.time() - last_dashboard >= DASHBOARD_INTERVAL_SECONDS:
            events = collect_upcoming_events(settings, engines, states, now,
                                             DASHBOARD_BUSINESS_DAYS)
            guarded_send(backend, render_dashboard(events, language), language)
            last_dashboard = time.time()

        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv) > 1 else "config/config.yaml")
