"""Save/load per-exchange phase state so restarts don't duplicate notifications."""

import json
import pathlib

from .logging import get_logger
from .state import ExchangeState, Phase

logger = get_logger()


def _path(data_dir: str, mic: str) -> pathlib.Path:
    return pathlib.Path(data_dir) / f"state_{mic}.json"


def save_state(data_dir: str, state: ExchangeState) -> None:
    p = _path(data_dir, state.cfg.mic)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase": state.current_phase.value,
        "last_transition": state.last_transition.isoformat() if state.last_transition else None,
        "exceptional_since": state.exceptional_since.isoformat() if state.exceptional_since else None,
        "active_halt_since": state.active_halt_since.isoformat() if state.active_halt_since else None,
    }
    p.write_text(json.dumps(payload), encoding="utf-8")


def load_state(data_dir: str, state: ExchangeState) -> None:
    p = _path(data_dir, state.cfg.mic)
    if not p.exists():
        return
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        state.current_phase = Phase(payload["phase"])
        if payload.get("exceptional_since"):
            state.exceptional_since = dt_fromiso(payload["exceptional_since"])
        if payload.get("active_halt_since"):
            state.active_halt_since = dt_fromiso(payload["active_halt_since"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Could not restore state for %s: %s", state.cfg.mic, exc)


def dt_fromiso(s: str):
    import datetime as dt
    return dt.datetime.fromisoformat(s)
