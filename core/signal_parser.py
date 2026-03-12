"""
SignalParser - converts raw Telegram messages into Signal objects.

Expected open signal format (human-written, some variation allowed):
  Sell XAUUSD
  1%
  E: 2318.50
  SL: 2305.00
  TP: OPEN        ← or TP: 2340.00 for fixed TP

Variations handled:
  - Extra words, emoji, punctuation around each line
  - "E:", "Entry:", "entry" for entry price
  - "SL:", "S/L:", "Stop:", "Stop loss:" for stop loss
  - "TP:", "T/P:", "Take profit:", "TP: open/OPEN/-" for take profit
  - "1%", "1% risk", "risk: 1%" for risk percent
  - "Gold" → XAUUSD, "BTC" → BTCUSD, etc.
"""

import re
from typing import Optional
from core.signal import Signal, Direction, OrderType
from core.logger import get_logger

logger = get_logger("parser")

# ── Normalisation maps ────────────────────────────────────────────────────────

DIRECTION_ALIASES = {
    "buy":  Direction.BUY,
    "long": Direction.BUY,
    "bull": Direction.BUY,
    "sell": Direction.SELL,
    "short": Direction.SELL,
    "bear": Direction.SELL,
}

SYMBOL_ALIASES = {
    "gold":    "XAUUSD",
    "silver":  "XAGUSD",
    "oil":     "XTIUSD",
    "btc":     "BTCUSD",
    "bitcoin": "BTCUSD",
    "eth":     "ETHUSD",
    "nasdaq":  "NAS100",
    "nas100":  "NAS100",
    "dj30":    "DJ30",
    "dow":     "DJ30",
    "sp500":   "SPX500",
    "us30":    "DJ30",
}

# Words that look like symbols but aren't — excluded from generic symbol detection
NOT_SYMBOLS = {
    "BUY", "SELL", "NOW", "LONG", "SHORT", "STOP", "ENTRY",
    "OPEN", "CLOSE", "RISK", "TAKE", "PROFIT", "LOSS",
}

# ── Regex building blocks ─────────────────────────────────────────────────────

# Price: digits with optional decimal (comma or dot)
PRICE_RE = r"\d+(?:[.,]\d+)?"

# Separator between label and value: optional spaces, colon, dash, pipe
SEP = r"[\s:=\-|]*\s*"

# ── Compiled patterns ─────────────────────────────────────────────────────────

# Entry price  — "E: 2318.50", "Entry: 1.0850", "entry 1.0850", "@ 2318"
ENTRY_RE = re.compile(
    rf"(?:e(?:ntry)?|enter|@|price){SEP}({PRICE_RE})",
    re.IGNORECASE,
)

# Stop loss — "SL: 2305", "S/L: 2305", "Stop: 2305", "Stop loss: 2305"
SL_RE = re.compile(
    rf"(?:s(?:top)?[\s/]*l(?:oss)?|stop){SEP}({PRICE_RE})",
    re.IGNORECASE,
)

# Take profit (fixed) — "TP: 2340", "T/P: 2340", "Take profit: 2340"
TP_FIXED_RE = re.compile(
    rf"(?:t(?:ake)?[\s/]*p(?:rofit)?){SEP}({PRICE_RE})",
    re.IGNORECASE,
)

# Take profit (open/none) — "TP: OPEN", "TP: -", "TP: open", "tp open"
TP_OPEN_RE = re.compile(
    rf"(?:t(?:ake)?[\s/]*p(?:rofit)?){SEP}(?:open|-+|none|n/?a)",
    re.IGNORECASE,
)

# Risk percent — "1%", "1% risk", "risk: 1%", "risk 1.5%", "0.5% risk"
RISK_RE = re.compile(
    r"(?:risk{SEP}(\d+(?:[.,]\d+)?)\s*%)|(?:(\d+(?:[.,]\d+)?)\s*%(?:\s*risk)?)".replace("{SEP}", SEP),
    re.IGNORECASE,
)


def _clean(price_str: str) -> float:
    return float(price_str.replace(",", "."))


class SignalParser:

    def parse(self, text: str, message_id: Optional[int] = None) -> Optional[Signal]:
        try:
            return self._parse(text, message_id)
        except Exception as e:
            logger.warning(f"Parser error: {e}")
            return None

    def _parse(self, text: str, message_id: Optional[int]) -> Optional[Signal]:
        t = text.strip()

        # ── Direction (required) ──────────────────────────────────────────────
        direction = self._extract_direction(t)
        if direction is None:
            return None

        # ── Symbol (required) ─────────────────────────────────────────────────
        symbol = self._extract_symbol(t)
        if symbol is None:
            logger.debug("Direction found but no symbol — skipping")
            return None

        # ── Entry price ───────────────────────────────────────────────────────
        # Primary: explicit "E: ..." label
        entry_match = ENTRY_RE.search(t)
        entry_price = _clean(entry_match.group(1)) if entry_match else None

        # ── Stop loss ─────────────────────────────────────────────────────────
        sl_match = SL_RE.search(t)
        stop_loss = _clean(sl_match.group(1)) if sl_match else None

        # ── Take profit ───────────────────────────────────────────────────────
        # Check for open TP first, then fixed price
        tp_is_open = bool(TP_OPEN_RE.search(t))
        if tp_is_open:
            take_profits = []   # open TP → no fixed target
        else:
            take_profits = [_clean(m.group(1)) for m in TP_FIXED_RE.finditer(t)]

        # ── Risk percent ──────────────────────────────────────────────────────
        risk_percent = None
        risk_match = RISK_RE.search(t)
        if risk_match:
            raw = risk_match.group(1) or risk_match.group(2)
            if raw:
                val = _clean(raw)
                if 0.1 <= val <= 10.0:
                    risk_percent = val
                else:
                    logger.warning(f"Risk % out of range ({val}) — ignoring")

        # ── Order type ────────────────────────────────────────────────────────
        # Entry price present → limit order; absent → market order
        order_type = OrderType.LIMIT if entry_price else OrderType.MARKET

        signal = Signal(
            direction=direction,
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profits=take_profits,
            order_type=order_type,
            risk_percent=risk_percent,
            raw_message=text,
            source_message_id=message_id,
        )

        logger.info(
            f"Parsed: {signal} | risk={risk_percent}% "
            f"| TP={'OPEN' if tp_is_open else take_profits or '—'}"
        )
        return signal

    # ── Extraction helpers ────────────────────────────────────────────────────

    def _extract_direction(self, text: str) -> Optional[Direction]:
        lower = text.lower()
        for alias, direction in DIRECTION_ALIASES.items():
            if re.search(rf"\b{alias}\b", lower):
                return direction
        return None

    def _extract_symbol(self, text: str) -> Optional[str]:
        lower = text.lower()
        upper = text.upper()

        # Named aliases first (gold, bitcoin, etc.)
        for alias, symbol in SYMBOL_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", lower):
                return symbol

        # Standard Forex / CFD pair (e.g. EURUSD, XAUUSD, GBPJPY, NAS100)
        pair_match = re.search(
            r"\b([A-Z]{2,6}(?:USD|JPY|GBP|EUR|CHF|AUD|CAD|NZD|BTC|ETH|XAU|XAG))\b",
            upper,
        )
        if pair_match:
            return pair_match.group(1)

        # Indices without currency suffix (NAS100, US30, DJ30, SPX500)
        index_match = re.search(
            r"\b(NAS100|US30|DJ30|SPX500|GER40|UK100|JPN225)\b",
            upper,
        )
        if index_match:
            return index_match.group(1)

        # Generic 3-7 char uppercase word as last resort
        for m in re.finditer(r"\b([A-Z]{3,7})\b", upper):
            candidate = m.group(1)
            if candidate not in NOT_SYMBOLS:
                return candidate

        return None


# ── Close signal patterns ─────────────────────────────────────────────────────

CLOSE_ALL_RE = re.compile(r"\bclose\s+all\b",  re.IGNORECASE)
CLOSE_RE     = re.compile(r"\bclose\b",         re.IGNORECASE)
CANCEL_RE    = re.compile(r"\bcancel\b",         re.IGNORECASE)

# Close price: "close now 2318.50", "closed @ 1.0842"
CLOSE_PRICE_RE = re.compile(
    rf"(?:closed?\s+(?:now\s+)?|@\s*)({PRICE_RE})",
    re.IGNORECASE,
)

# Realized pips: "+32 pips", "-10pips", "32.5 pips"
CLOSE_PIPS_RE = re.compile(
    r"([+-]?\s*\d+(?:[.,]\d+)?)\s*pips?",
    re.IGNORECASE,
)


class CloseSignalParser:

    def is_close_message(self, text: str) -> bool:
        return bool(CLOSE_RE.search(text) or CANCEL_RE.search(text))

    def parse(
        self,
        text: str,
        message_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ):
        from core.signal import CloseSignal, CloseType
        try:
            return self._parse(text, message_id, reply_to_message_id)
        except Exception as e:
            logger.warning(f"CloseSignalParser error: {e}")
            return None

    def _parse(self, text, message_id, reply_to_message_id):
        from core.signal import CloseSignal, CloseType

        # ── Close all ─────────────────────────────────────────────────────────
        if CLOSE_ALL_RE.search(text):
            logger.info(f"Close-all signal: msg_id={message_id}")
            return CloseSignal(
                close_type=CloseType.CLOSE_ALL,
                raw_message=text,
                source_message_id=message_id,
            )

        # ── Cancel pending order (reply-linked) ───────────────────────────────
        if CANCEL_RE.search(text) and not CLOSE_RE.search(text):
            price_match = CLOSE_PRICE_RE.search(text)
            logger.info(f"Cancel signal: reply_to={reply_to_message_id}")
            return CloseSignal(
                close_type=CloseType.CANCEL,
                reply_to_message_id=reply_to_message_id,
                close_price=_clean(price_match.group(1)) if price_match else None,
                raw_message=text,
                source_message_id=message_id,
            )

        # ── Close positions for symbol ────────────────────────────────────────
        if CLOSE_RE.search(text):
            price_match = CLOSE_PRICE_RE.search(text)
            pips_match  = CLOSE_PIPS_RE.search(text)
            close_price   = _clean(price_match.group(1)) if price_match else None
            realized_pips = None
            if pips_match:
                realized_pips = float(pips_match.group(1).replace(" ", "").replace(",", "."))

            # Try to extract symbol from the close message
            signal_parser = SignalParser()
            symbol = signal_parser._extract_symbol(text)

            logger.info(
                f"Close signal: symbol={symbol} price={close_price} "
                f"pips={realized_pips} reply_to={reply_to_message_id}"
            )
            return CloseSignal(
                close_type=CloseType.CLOSE,
                symbol=symbol,
                reply_to_message_id=reply_to_message_id,
                close_price=close_price,
                realized_pips=realized_pips,
                raw_message=text,
                source_message_id=message_id,
            )

        return None
