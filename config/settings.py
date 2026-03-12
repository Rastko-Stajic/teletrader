"""
Settings - loads all config from .env file
"""

from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv
import os

load_dotenv()


@dataclass
class Settings:
    # --- Telegram ---
    telegram_api_id: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_API_ID", "0")))
    telegram_api_hash: str = field(default_factory=lambda: os.getenv("TELEGRAM_API_HASH", ""))
    telegram_phone: str = field(default_factory=lambda: os.getenv("TELEGRAM_PHONE", ""))
    telegram_group_id: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_GROUP_ID", "0")))

    # --- MT5 ---
    mt5_login: int = field(default_factory=lambda: int(os.getenv("MT5_LOGIN", "0")))
    mt5_password: str = field(default_factory=lambda: os.getenv("MT5_PASSWORD", ""))
    mt5_server: str = field(default_factory=lambda: os.getenv("MT5_SERVER", ""))

    # --- Risk Management ---
    max_lot_size: float = field(default_factory=lambda: float(os.getenv("MAX_LOT_SIZE", "0.1")))
    default_lot_size: float = field(default_factory=lambda: float(os.getenv("DEFAULT_LOT_SIZE", "0.01")))
    max_open_trades: int = field(default_factory=lambda: int(os.getenv("MAX_OPEN_TRADES", "5")))
    max_daily_loss_usd: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_USD", "100")))
    kill_switch: bool = False  # toggled at runtime via dashboard

    # --- Symbols whitelist (empty = allow all) ---
    allowed_symbols: List[str] = field(
        default_factory=lambda: [
            s.strip() for s in os.getenv("ALLOWED_SYMBOLS", "").split(",") if s.strip()
        ]
    )

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
        if not self.mt5_login:
            errors.append("MT5_LOGIN missing")
        if not self.mt5_password:
            errors.append("MT5_PASSWORD missing")
        if not self.mt5_server:
            errors.append("MT5_SERVER missing")
        return errors
