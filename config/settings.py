"""
Settings - two config classes:

  SharedSettings  - loaded by signal_service.py from .env
                    Telegram credentials, dashboard config

  AccountSettings - loaded by trader.py from accounts/accountX.env
                    MT5 credentials, risk management, broker-specific config
"""

from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv
import os


def _load_env(path: str = None):
    """Load .env from a specific path or default location."""
    if path:
        load_dotenv(dotenv_path=path, override=True)
    else:
        load_dotenv()


@dataclass
class SharedSettings:
    """Config for signal_service.py — Telegram + dashboard only."""

    # --- Telegram ---
    telegram_api_id:   int = field(default_factory=lambda: int(os.getenv("TELEGRAM_API_ID", "0")))
    telegram_api_hash: str = field(default_factory=lambda: os.getenv("TELEGRAM_API_HASH", ""))
    telegram_phone:    str = field(default_factory=lambda: os.getenv("TELEGRAM_PHONE", ""))
    telegram_group_id: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_GROUP_ID", "0")))

    # --- ZeroMQ signal broadcast ---
    zmq_host: str = field(default_factory=lambda: os.getenv("ZMQ_HOST", "127.0.0.1"))
    zmq_port: int = field(default_factory=lambda: int(os.getenv("ZMQ_PORT", "5555")))

    # --- Dashboard ---
    dashboard_host: str = field(default_factory=lambda: os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    dashboard_port: int = field(default_factory=lambda: int(os.getenv("DASHBOARD_PORT", "8080")))

    def validate(self) -> List[str]:
        errors = []
        if not self.telegram_api_id:
            errors.append("TELEGRAM_API_ID missing")
        if not self.telegram_api_hash:
            errors.append("TELEGRAM_API_HASH missing")
        if not self.telegram_phone:
            errors.append("TELEGRAM_PHONE missing")
        if not self.telegram_group_id:
            errors.append("TELEGRAM_GROUP_ID missing")
        return errors


@dataclass
class AccountSettings:
    """Config for trader.py — one instance per MT5 account."""

    # --- Account identity ---
    account_label: str = field(default_factory=lambda: os.getenv("ACCOUNT_LABEL", "Account"))

    # --- MT5 connection ---
    mt5_login:         int = field(default_factory=lambda: int(os.getenv("MT5_LOGIN", "0")))
    mt5_password:      str = field(default_factory=lambda: os.getenv("MT5_PASSWORD", ""))
    mt5_server:        str = field(default_factory=lambda: os.getenv("MT5_SERVER", ""))
    mt5_terminal_path: str = field(default_factory=lambda: os.getenv("MT5_TERMINAL_PATH", ""))
    mt5_symbol_suffix: str = field(default_factory=lambda: os.getenv("MT5_SYMBOL_SUFFIX", ".bp"))

    # --- ZeroMQ signal subscription ---
    zmq_host: str = field(default_factory=lambda: os.getenv("ZMQ_HOST", "127.0.0.1"))
    zmq_port: int = field(default_factory=lambda: int(os.getenv("ZMQ_PORT", "5555")))

    # --- Risk management ---
    max_lot_size:          float = field(default_factory=lambda: float(os.getenv("MAX_LOT_SIZE", "1.5")))
    default_lot_size:      float = field(default_factory=lambda: float(os.getenv("DEFAULT_LOT_SIZE", "0.01")))
    max_open_trades:       int   = field(default_factory=lambda: int(os.getenv("MAX_OPEN_TRADES", "5")))
    max_daily_loss_usd:    float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_USD", "100")))
    force_market_execution: bool = field(default_factory=lambda: os.getenv("FORCE_MARKET_EXECUTION", "true").lower() == "true")
    kill_switch:           bool  = False   # toggled at runtime via dashboard
    gold_enabled:          bool  = field(default_factory=lambda: os.getenv("GOLD_ENABLED", "true").lower() == "true")

    # --- Symbols whitelist (empty = allow all) ---
    allowed_symbols: List[str] = field(
        default_factory=lambda: [
            s.strip() for s in os.getenv("ALLOWED_SYMBOLS", "").split(",") if s.strip()
        ]
    )

    # --- Trader dashboard (optional per-account dashboard) ---
    dashboard_host: str = field(default_factory=lambda: os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    dashboard_port: int = field(default_factory=lambda: int(os.getenv("DASHBOARD_PORT", "8081")))

    def validate(self) -> List[str]:
        errors = []
        if not self.mt5_login:
            errors.append("MT5_LOGIN missing")
        if not self.mt5_password:
            errors.append("MT5_PASSWORD missing")
        if not self.mt5_server:
            errors.append("MT5_SERVER missing")
        return errors


# ── Convenience loaders ───────────────────────────────────────────────────────

def load_shared_settings(env_path: str = None) -> SharedSettings:
    _load_env(env_path)
    return SharedSettings()


def load_account_settings(env_path: str = None) -> AccountSettings:
    _load_env(env_path)
    return AccountSettings()
