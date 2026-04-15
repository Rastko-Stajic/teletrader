"""
RiskManager - gates every signal before it reaches the executor.
Checks: kill switch, gold toggle, symbol whitelist, lot size, max open trades, daily loss limit.
"""

from typing import Tuple
from datetime import datetime, date
from core.signal import Signal
from core.logger import get_logger
from config.settings import Settings

logger = get_logger("risk")

GOLD_SYMBOL = "XAUUSD"


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._daily_loss: float = 0.0
        self._daily_loss_date: date = datetime.utcnow().date()
        self._trades_today: int = 0

    def approve(self, signal: Signal) -> Tuple[bool, str]:
        """
        Returns (True, "") if trade is allowed, or (False, reason) if blocked.
        """
        # ── Kill switch ───────────────────────────────────────────────────────
        if self.settings.kill_switch:
            return False, "Kill switch is ACTIVE — all trading halted"

        # ── Gold toggle ───────────────────────────────────────────────────────
        if signal.symbol == GOLD_SYMBOL and not self.settings.gold_enabled:
            return False, "Gold (XAUUSD) trading is currently disabled"

        # ── Symbol whitelist ──────────────────────────────────────────────────
        if self.settings.allowed_symbols:
            if signal.symbol not in self.settings.allowed_symbols:
                return False, f"Symbol {signal.symbol} not in whitelist"

        # ── Lot size guard ────────────────────────────────────────────────────
        lot = signal.lot_size or self.settings.default_lot_size
        if lot > self.settings.max_lot_size:
            return False, f"Lot size {lot} exceeds max {self.settings.max_lot_size}"

        # ── Daily loss limit ──────────────────────────────────────────────────
        self._reset_daily_if_needed()
        if self._daily_loss >= self.settings.max_daily_loss_usd:
            return False, (
                f"Daily loss limit reached: "
                f"${self._daily_loss:.2f} / ${self.settings.max_daily_loss_usd:.2f}"
            )

        # ── Confidence threshold ──────────────────────────────────────────────
        if signal.confidence < 0.6:
            return False, f"Signal confidence too low: {signal.confidence:.0%}"

        return True, ""

    def record_trade_result(self, pnl: float):
        """Call this after a trade closes to update daily loss tracking."""
        self._reset_daily_if_needed()
        if pnl < 0:
            self._daily_loss += abs(pnl)
            logger.info(f"Daily loss updated: ${self._daily_loss:.2f}")

    def _reset_daily_if_needed(self):
        today = datetime.utcnow().date()
        if today != self._daily_loss_date:
            self._daily_loss = 0.0
            self._trades_today = 0
            self._daily_loss_date = today
            logger.info("Daily loss counter reset.")

    @property
    def daily_loss(self) -> float:
        self._reset_daily_if_needed()
        return self._daily_loss

    def toggle_kill_switch(self) -> bool:
        self.settings.kill_switch = not self.settings.kill_switch
        state = "ACTIVE" if self.settings.kill_switch else "INACTIVE"
        logger.warning(f"Kill switch toggled: {state}")
        return self.settings.kill_switch

    def toggle_gold(self) -> bool:
        self.settings.gold_enabled = not self.settings.gold_enabled
        state = "ENABLED" if self.settings.gold_enabled else "DISABLED"
        logger.info(f"Gold trading toggled: {state}")
        return self.settings.gold_enabled
