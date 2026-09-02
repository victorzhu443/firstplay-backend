"""
Logging configuration.

The application had no logging at all — startup used print() and everything
else was silent, so a retry that succeeded on its second attempt, a node that
halted the pipeline, or a rejected API key left no trace. The modules already
log through the stdlib logger; this makes that output actually appear.

Render captures stdout, so a stream handler is all that is needed. Format is
deliberately flat rather than JSON: these logs are read by a person scrolling
Render's log tail, not shipped to an aggregator.
"""
import logging
import os
import sys

DEFAULT_LEVEL = "INFO"

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str = None) -> None:
    """
    Install a single stdout handler for the application's loggers.

    Idempotent: calling it twice will not attach duplicate handlers, which
    would print every line as many times as it was called.

    Args:
        level: Log level name; defaults to $LOG_LEVEL, then INFO
    """
    resolved = (level or os.getenv("LOG_LEVEL") or DEFAULT_LEVEL).upper()

    root = logging.getLogger()
    root.setLevel(resolved)

    # Replace our own handler rather than adding another, and leave any
    # handler installed by the host (uvicorn, pytest) alone.
    for handler in list(root.handlers):
        if getattr(handler, "_firstplay", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler._firstplay = True
    root.addHandler(handler)

    # httpx logs every outbound request at INFO, which at this volume is one
    # line per LLM call and drowns the application's own output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
