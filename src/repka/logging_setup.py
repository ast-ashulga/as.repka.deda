"""Logging: readable lines on stderr plus a rotating file under logs/."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from repka.config import logs_dir

LOG_FILE_NAME = "repka.log"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", verbose: bool = False) -> None:
    """Configure the `repka` logger. verbose forces DEBUG. Calling it again replaces the handlers."""
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        raise ValueError(f"bad log level {level!r}")
    if verbose:
        numeric = logging.DEBUG

    log_dir = logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("repka")
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()

    handlers = [
        logging.StreamHandler(sys.stderr),
        RotatingFileHandler(log_dir / LOG_FILE_NAME, maxBytes=1_000_000, backupCount=5, encoding="utf-8"),
    ]
    for handler in handlers:
        handler.setLevel(numeric)
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(numeric)
    logger.propagate = False
