"""
Signal - trade instructions parsed from Telegram messages.

Two types:
  Signal      — open a new position
  CloseSignal — close/cancel an existing position
"""

from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT  = "LIMIT"
    STOP   = "STOP"


class CloseType(str, Enum):
    CLOSE     = "CLOSE"      # close an open position (market close)
    CANCEL    = "CANCEL"     # delete a pending order
    CLOSE_ALL = "CLOSE_ALL"  # close every open position regardless of symbol


@dataclass
class Signal:
    # Required
    direction: Direction
    symbol: str

    # Optional price levels
    entry_price:  Optional[float] = None
    stop_loss:    Optional[float] = None
    take_profits: List[float] = field(default_factory=list)

    # Order type
    order_type: OrderType = OrderType.MARKET

    # Risk & lot sizing
    risk_percent: Optional[float] = None   # e.g. 1.0 → 1%, parsed from message
    lot_size:     Optional[float] = None   # filled by LotCalculator before execution

    # Metadata
    raw_message:       str = ""
    confidence:        float = 1.0
    timestamp:         datetime = field(default_factory=datetime.utcnow)
    source_message_id: Optional[int] = None

    def __str__(self):
        tp_str = " / ".join(str(t) for t in self.take_profits) if self.take_profits else "—"
        return (
            f"{self.direction.value} {self.symbol} "
            f"@ {'MKT' if not self.entry_price else self.entry_price} | "
            f"SL: {self.stop_loss or '—'} | TP: {tp_str}"
        )


@dataclass
class CloseSignal:
    close_type: CloseType

    # Symbol to close all positions for (used by CLOSE).
    # None only for CLOSE_ALL (which closes everything).
    symbol: Optional[str] = None

    # Still needed for CANCEL — reply links to the specific pending order.
    reply_to_message_id: Optional[int] = None

    # Informational fields parsed from the close message
    close_price:   Optional[float] = None
    realized_pips: Optional[float] = None

    # Metadata
    raw_message:       str = ""
    timestamp:         datetime = field(default_factory=datetime.utcnow)
    source_message_id: Optional[int] = None

    def __str__(self):
        if self.close_type == CloseType.CLOSE_ALL:
            return "CLOSE ALL positions"
        sym   = f" {self.symbol}" if self.symbol else ""
        price = f" @ {self.close_price}" if self.close_price else ""
        pips  = f" ({self.realized_pips:+.1f} pips)" if self.realized_pips is not None else ""
        return f"{self.close_type.value}{sym}{price}{pips}"
