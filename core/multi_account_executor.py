"""
MultiAccountExecutor - executes trades across multiple MT5 accounts.

Each account gets its own MT5Executor instance with its own connection.
Lot sizes are calculated independently per account based on each account's balance.
Failures on one account are logged but do not stop execution on others.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from copy import deepcopy

from core.mt5_executor import MT5Executor
from core.lot_calculator import get_lot_size
from core.signal import Signal, OrderType
from core.logger import get_logger
from config.settings import Settings

logger = get_logger("multi_account")


@dataclass
class AccountConfig:
    login:    int
    password: str
    server:   str
    label:    str             # e.g. "Account 1 (primary)", "Account 2"
    path:     str = ""        # path to terminal64.exe (optional)


def parse_extra_accounts(raw: str) -> List[AccountConfig]:
    """
    Parse MT5_EXTRA_ACCOUNTS env string into AccountConfig list.

    Format: "login:password:server|C:\\path\\terminal64.exe,login2:password2:server2"

    Rules:
      - Accounts are comma-separated
      - Within each account: credentials use ":" as separator
      - Terminal path (optional) is separated from credentials by "|"
      - Spaces in path are fine — no quotes needed
    """
    configs = []
    if not raw.strip():
        return configs
    for i, entry in enumerate(raw.split(","), start=2):
        entry = entry.strip()

        # Split off optional terminal path (pipe separator)
        path = ""
        if "|" in entry:
            credentials, path = entry.split("|", 1)
            path = path.strip()
        else:
            credentials = entry

        parts = credentials.strip().split(":")
        if len(parts) < 3:
            logger.warning(f"Invalid MT5_EXTRA_ACCOUNTS entry (skipping): '{entry}'")
            continue

        login    = parts[0].strip()
        password = parts[1].strip()
        server   = ":".join(parts[2:]).strip()

        try:
            configs.append(AccountConfig(
                login=int(login),
                password=password,
                server=server,
                label=f"Account {i}",
                path=path,
            ))
        except ValueError:
            logger.warning(f"Invalid login in MT5_EXTRA_ACCOUNTS (skipping): '{login}'")
    return configs


def build_executors(settings: Settings) -> List[MT5Executor]:
    """
    Build one MT5Executor per account (primary + extras).
    Returns list with primary account first.
    """
    # Primary account
    primary_settings = deepcopy(settings)
    primary_settings._label = "Account 1 (primary)"
    executors = [MT5Executor(primary_settings, label="Account 1 (primary)")]

    # Extra accounts
    extra_configs = parse_extra_accounts(settings.mt5_extra_accounts)
    for cfg in extra_configs:
        acct_settings = deepcopy(settings)
        acct_settings.mt5_login    = cfg.login
        acct_settings.mt5_password = cfg.password
        acct_settings.mt5_server   = cfg.server
        executors.append(MT5Executor(acct_settings, label=cfg.label, terminal_path=cfg.path))

    return executors


class MultiAccountExecutor:
    """
    Drop-in replacement for MT5Executor that fans out operations
    to all configured accounts.
    """

    def __init__(self, executors: List[MT5Executor]):
        self.executors = executors

    def connect_all(self) -> bool:
        """Connect all accounts. Returns True if at least one succeeds."""
        any_ok = False
        for ex in self.executors:
            ok = ex.connect()
            if ok:
                any_ok = True
            else:
                logger.error(f"[{ex.label}] Failed to connect — will be skipped")
                ex.connected = False
        return any_ok

    def _active(self) -> List[MT5Executor]:
        return [ex for ex in self.executors if ex.connected]

    # ── Open ─────────────────────────────────────────────────────────────────

    async def execute_all(self, signal: Signal, risk_percent: float) -> List[Dict[str, Any]]:
        """
        Execute the signal on all connected accounts.
        Lot size is calculated independently per account.
        Returns list of results, one per account.
        """
        results = []
        for ex in self._active():
            result = await self._execute_on_account(ex, signal, risk_percent)
            results.append({"label": ex.label, **result})
        return results

    async def _execute_on_account(
        self, ex: MT5Executor, signal: Signal, risk_percent: float
    ) -> Dict[str, Any]:
        try:
            # Deep copy signal so per-account lot/price changes don't bleed across
            acct_signal = deepcopy(signal)

            # Get this account's balance
            account_info = ex.get_account_info()
            balance = account_info.get("balance", 0)
            currency = account_info.get("currency", "USD")

            if balance <= 0:
                return {"success": False, "error": f"[{ex.label}] Balance unavailable"}

            # Get live price for this account
            live_price = ex.get_live_price(acct_signal.symbol, acct_signal.direction.value)
            if live_price is None:
                return {"success": False, "error": f"[{ex.label}] Cannot get live price"}

            # Calculate lot size for this account's balance
            lot = await get_lot_size(
                balance=balance,
                risk_percent=risk_percent,
                entry_price=live_price,
                stop_loss_price=acct_signal.stop_loss,
                symbol=acct_signal.symbol,
                account_currency=currency,
            )
            if lot <= 0:
                return {"success": False, "error": f"[{ex.label}] Lot size {lot} invalid"}

            acct_signal.lot_size    = lot
            acct_signal.entry_price = live_price
            acct_signal.order_type  = OrderType.MARKET

            logger.info(
                f"[{ex.label}] balance=${balance:.2f} → "
                f"lot={lot} @ {live_price}"
            )

            result = ex.execute(acct_signal)
            return result

        except Exception as e:
            logger.error(f"[{ex.label}] Unexpected error during execute: {e}")
            return {"success": False, "error": str(e)}

    # ── Close ─────────────────────────────────────────────────────────────────

    def close_all_positions(self) -> Dict[str, Any]:
        closed, failed = [], []
        for ex in self._active():
            result = ex.close_all_positions()
            closed.extend(result.get("closed", []))
            failed.extend(result.get("failed", []))
        return {"success": len(failed) == 0, "closed": closed, "failed": failed}

    def close_positions_by_symbol(self, symbol: str) -> Dict[str, Any]:
        closed, failed = [], []
        for ex in self._active():
            result = ex.close_positions_by_symbol(symbol)
            closed.extend(result.get("closed", []))
            failed.extend(result.get("failed", []))
        return {"success": len(failed) == 0, "closed": closed, "failed": failed}

    def close_positions_by_symbol_and_direction(self, symbol: str, direction: str) -> Dict[str, Any]:
        closed, failed = [], []
        for ex in self._active():
            result = ex.close_positions_by_symbol_and_direction(symbol, direction)
            closed.extend(result.get("closed", []))
            failed.extend(result.get("failed", []))
        return {"success": len(failed) == 0, "closed": closed, "failed": failed}

    def cancel_pending_order(self, ticket: int) -> Dict[str, Any]:
        # Cancel on all accounts — ticket may exist on any of them
        success = False
        for ex in self._active():
            result = ex.cancel_pending_order(ticket)
            if result["success"]:
                success = True
        return {"success": success}

    # ── Move SL ───────────────────────────────────────────────────────────────

    def modify_sl_by_symbol_and_direction(
        self, symbol: str, direction: str, new_sl: float
    ) -> Dict[str, Any]:
        modified, failed = [], []
        for ex in self._active():
            result = ex.modify_sl_by_symbol_and_direction(symbol, direction, new_sl)
            modified.extend(result.get("modified", []))
            failed.extend(result.get("failed", []))
        return {"success": len(failed) == 0, "modified": modified, "failed": failed}

    # ── Info (primary account) ─────────────────────────────────────────────────
    # These use only the primary account since they're for display/calculation

    def get_account_info(self) -> Dict[str, Any]:
        return self.executors[0].get_account_info() if self.executors else {}

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Returns positions from all accounts combined."""
        all_positions = []
        for ex in self._active():
            all_positions.extend(ex.get_open_positions())
        return all_positions

    def get_live_price(self, symbol: str, direction: str) -> Optional[float]:
        """Use primary account for live price."""
        return self.executors[0].get_live_price(symbol, direction) if self.executors else None