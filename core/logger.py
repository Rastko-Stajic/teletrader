"""
Logger - structured logging to console + rotating file.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

_loggers = {}

# ── Unrecognized message log ──────────────────────────────────────────────────

import json as _json

UNRECOGNIZED_LOG = os.path.join(LOG_DIR, "unrecognized.log")

def log_unrecognized(text: str, message_id: int):
    """
    Append an unrecognized Telegram message to unrecognized.log (one JSON per line)
    and push it to the dashboard state for human review.
    """
    from datetime import datetime
    entry = {
        "message_id": message_id,
        "text": text,
        "timestamp": datetime.utcnow().isoformat(),
    }
    # File log
    try:
        with open(UNRECOGNIZED_LOG, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        get_logger("logger").error(f"Failed to write unrecognized log: {e}")

    # Dashboard state
    try:
        from ui.dashboard import push_unrecognized
        push_unrecognized(entry)
    except Exception:
        pass  # dashboard may not be running during tests

    get_logger("telegram").info(
        f"Unrecognized message [{message_id}] logged for review: {text[:60]}..."
    )

def get_logger(name: str) -> logging.Logger:
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(f"teletrader.{name}")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file handler (5 MB × 3 files)
    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "teletrader.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    _loggers[name] = logger
    return logger
