"""Pure phase computation for a given instant."""

import datetime as dt

from .calendars import CalendarEngine
from .config import ExchangeConfig
from .state import ExchangeState, Phase


def _parse_hhmm(s: str) -> dt.time:
    hh, mm = s.split(":")
    return dt.time(int(hh), int(mm))


def compute_phase(cfg: ExchangeConfig, engine: CalendarEngine,
                  state: ExchangeState, now: dt.datetime
                  ) -> tuple[Phase, dt.datetime | None, Phase | None]:
    """Return (current_phase, next_transition_dt, next_transition_phase)."""
    local_now = now.astimezone(dt.timezone(dt.timedelta(hours=0)))  # placeholder, replaced below
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg.tz)
    local_now = now.astimezone(tz)
    today = local_now.date()

    if not engine.is_trading_day(today):
        nxt = _find_next_trading_day(engine, today)
        return Phase.HOLIDAY, nxt, Phase.PRE_MARKET

    early_closes = engine.early_close_days_in(today, today)
    session = engine.session_for(today)
    if session is None:
        nxt = _find_next_trading_day(engine, today)
        return Phase.HOLIDAY, nxt, Phase.PRE_MARKET

    if today in early_closes:
        session.close_at = dt.datetime.combine(today, early_closes[today], tzinfo=session.close_at.tzinfo)

    s = cfg.session
    if s is None:
        return Phase.CLOSED, session.open_at, Phase.REGULAR

    pre = (dt.datetime.combine(today, _parse_hhmm(s.pre_market), tzinfo=tz)
           if s.pre_market else None)
    oa = (dt.datetime.combine(today, _parse_hhmm(s.opening_auction), tzinfo=tz)
          if s.opening_auction else None)
    ca = (dt.datetime.combine(today, _parse_hhmm(s.closing_auction), tzinfo=tz)
          if s.closing_auction else None)
    pm = (dt.datetime.combine(today, _parse_hhmm(s.post_market), tzinfo=tz)
          if s.post_market else None)

    lunch = None
    if cfg.lunch_break:
        lunch = (
            dt.datetime.combine(today, _parse_hhmm(cfg.lunch_break.start), tzinfo=tz),
            dt.datetime.combine(today, _parse_hhmm(cfg.lunch_break.end), tzinfo=tz),
        )

    if pm and local_now >= pm:
        return Phase.CLOSED, _next_session_open(engine, today), Phase.PRE_MARKET
    if local_now >= session.close_at:
        nxt_phase = Phase.CLOSED
        return (Phase.EARLY_CLOSE if today in early_closes else Phase.POST_MARKET
                if pm else Phase.CLOSED), (pm if pm else _next_session_open(engine, today)), nxt_phase
    if ca and local_now >= ca:
        return Phase.CLOSING_AUCTION, session.close_at, Phase.POST_MARKET if pm else Phase.CLOSED
    if lunch and lunch[0] <= local_now < lunch[1]:
        return Phase.LUNCH, lunch[1], Phase.REGULAR
    if local_now >= session.open_at:
        nxt = lunch[0] if lunch else (ca if ca else session.close_at)
        nxt_ph = Phase.LUNCH if lunch else (Phase.CLOSING_AUCTION if ca else
                                            Phase.POST_MARKET if pm else Phase.CLOSED)
        return Phase.REGULAR, nxt, nxt_ph
    if oa and local_now >= oa:
        return Phase.OPENING_AUCTION, session.open_at, Phase.REGULAR
    if pre and local_now >= pre:
        nxt = oa if oa else session.open_at
        nxt_ph = Phase.OPENING_AUCTION if oa else Phase.REGULAR
        return Phase.PRE_MARKET, nxt, nxt_ph

    # before pre-market: closed, next event is today's pre-market or open
    first = pre if pre else (oa if oa else session.open_at)
    first_ph = Phase.PRE_MARKET if pre else (Phase.OPENING_AUCTION if oa else Phase.REGULAR)
    return Phase.CLOSED, first, first_ph


def _find_next_trading_day(engine: CalendarEngine, after: dt.date,
                           max_look: int = 30) -> dt.datetime | None:
    day = after + dt.timedelta(days=1)
    for _ in range(max_look):
        sess = engine.session_for(day)
        if sess is not None:
            return sess.open_at
        day += dt.timedelta(days=1)
    return None


def _next_session_open(engine: CalendarEngine, today: dt.date) -> dt.datetime | None:
    return _find_next_trading_day(engine, today)
