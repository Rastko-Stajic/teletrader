"""
PositionTracker - the bridge between Telegram message IDs and MT5 tickets.

When the bot opens a trade it records:
    telegram_message_id  →  mt5_ticket

When a close signal arrives as a reply to a previous message, we look up the
original message ID to find the corresponding MT5 ticket.

Storage is in-memory (survives the session) and persisted to a JSON file so
the mapping survives bot restarts.
"""

import json
import os
from typing import Optional, List, Dict, Any
from datetime import datetime
from core.logger import get_logger

logger = get_logger("tracker")

TRACKER_FILE = "logs/position_tracker.json"


class PositionTracker:
    def __init__(self):
        # telegram_message_id (str) → trade record dict
        self._records: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ── Write ─────────────────────────────────────────────────────────────────

    def record_open(
        self,
        telegram_message_id: int,
        mt5_ticket: int,
        symbol: str,
        direction: str,
        lot: float,
        entry_price: float,
    ):
        """Call this immediately after a trade is successfully opened in MT5."""
        key = str(telegram_message_id)
        self._records[key] = {
            "telegram_message_id": telegram_message_id,
            "mt5_ticket":          mt5_ticket,
            "symbol":              symbol,
            "direction":           direction,
            "lot":                 lot,
            "entry_price":         entry_price,
            "opened_at":           datetime.utcnow().isoformat(),
            "closed":              False,
            "closed_at":           None,
            "close_price":         None,
            "realized_pips":       None,
        }
        self._save()
        logger.info(
            f"Tracked: msg_id={telegram_message_id} → ticket={mt5_ticket} "
            f"({direction} {lot} {symbol} @ {entry_price})"
        )

    def record_close(
        self,
        telegram_message_id: int,
        close_price: Optional[float] = None,
        realized_pips: Optional[float] = None,
    ):
        """Mark a tracked position as closed."""
        key = str(telegram_message_id)
        if key not in self._records:
            logger.warning(f"record_close: no record for msg_id={telegram_message_id}")
            return
        self._records[key]["closed"]        = True
        self._records[key]["closed_at"]     = datetime.utcnow().isoformat()
        self._records[key]["close_price"]   = close_price
        self._records[key]["realized_pips"] = realized_pips
        self._save()
        logger.info(f"Closed record: msg_id={telegram_message_id} @ {close_price}")

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_ticket(self, telegram_message_id: int) -> Optional[int]:
        """Return the MT5 ticket for a given Telegram open-signal message ID."""
        record = self._records.get(str(telegram_message_id))
        if record and not record["closed"]:
            return record["mt5_ticket"]
        return None

    def get_record(self, telegram_message_id: int) -> Optional[Dict[str, Any]]:
        return self._records.get(str(telegram_message_id))

    def all_open_records(self) -> List[Dict[str, Any]]:
        return [r for r in self._records.values() if not r["closed"]]

    def all_records(self) -> List[Dict[str, Any]]:
        return list(self._records.values())

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self):
        os.makedirs(os.path.dirname(TRACKER_FILE), exist_ok=True)
        try:
            with open(TRACKER_FILE, "w", encoding="utf-8") as f:
                json.dump(self._records, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save tracker: {e}")

    def _load(self):
        if not os.path.exists(TRACKER_FILE):
            return
        try:
            with open(TRACKER_FILE, "r", encoding="utf-8") as f:
                self._records = json.load(f)
            open_count = sum(1 for r in self._records.values() if not r["closed"])
            logger.info(f"Tracker loaded: {len(self._records)} records, {open_count} open")
        except Exception as e:
            logger.error(f"Failed to load tracker: {e}")
            self._records = {}
