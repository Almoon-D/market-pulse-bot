"""Centralized logging configuration (std logging, no magic loggers)."""

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_configured = False


def configure(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _configured = True


def get_logger(name: str = "mercados") -> logging.Logger:
    configure()
    return logging.getLogger(name)
