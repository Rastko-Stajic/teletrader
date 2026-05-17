"""
LotCalculator - computes position size (standard lots) from:
    - account balance (from MT5)
    - risk % (from Telegram signal)
    - stop loss distance in pips (derived from signal entry vs SL prices)

Pip value is fetched LIVE from MT5 using:
    trade_tick_value / trade_tick_size × pip_size

This gives the exact pip value in account currency (USD) for any symbol,
including cross pairs and exotic instruments — no hardcoded approximations.

Falls back to hardcoded approximations only if MT5 is unavailable
(simulation mode / development on non-Windows machine).
"""

from __future__ import annotations

import math
from typing import Optional
from core.logger import get_logger

logger = get_logger("calculator")

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

# ── Pip size lookup ───────────────────────────────────────────────────────────
# Only needed for the fallback path (simulation mode).
# In live mode, pip size is still needed to convert price distance → pips,
# but pip VALUE comes from MT5 directly.

CUSTOM_PIP_SIZE: dict[str, float] = {
    # JPY pairs
    "USDJPY": 0.01, "EURJPY": 0.01, "GBPJPY": 0.01, "AUDJPY": 0.01,
    "CADJPY": 0.01, "CHFJPY": 0.01, "NZDJPY": 0.01, "SGDJPY": 0.01,
    # Gold / Silver
    "XAUUSD": 0.10,
    "XAGUSD": 0.001,
    # Indices — 1 point per pip
    "NAS100": 1.0, "US30": 1.0, "DJ30": 1.0,
    "SPX500": 1.0, "GER40": 1.0, "UK100": 1.0,
}

# Fallback lot units for simulation mode
LOT_UNITS: dict[str, float] = {
    "XAUUSD": 100.0,
    "XAGUSD": 5000.0,
    "NAS100": 1.0, "US30": 1.0, "DJ30": 1.0, "SPX500": 1.0,
}
DEFAULT_LOT_UNITS = 100_000.0


def pip_size(symbol: str) -> float:
    """
    Return the pip size for a symbol (size of 1 pip movement in price terms).
    Strips broker suffix before lookup (e.g. GBPUSD.bp → GBPUSD).
    """
    clean = _strip_suffix(symbol)
    return CUSTOM_PIP_SIZE.get(clean.upper(), 0.0001)


def _strip_suffix(symbol: str) -> str:
    """Remove broker suffix for pip size lookup (e.g. XAUUSD.bp → XAUUSD)."""
    if "." in symbol:
        return symbol.split(".")[0]
    return symbol


def price_to_pips(symbol: str, price_distance: float) -> float:
    """Convert a raw price difference to pips for the given symbol."""
    ps = pip_size(symbol)
    return round(abs(price_distance) / ps, 1)


# ── Live pip value from MT5 ───────────────────────────────────────────────────

def get_pip_value_from_mt5(symbol: str) -> Optional[float]:
    """
    Fetch the exact pip value (in account currency, e.g. USD) per 1 standard lot
    directly from MT5 using live tick value data.

    Formula:
        pip_value_per_lot = (pip_size / tick_size) × tick_value_per_lot

    Where:
        tick_size        = minimum price movement (from MT5 symbol info)
        tick_value       = value of 1 tick movement per 1 lot in account currency
        pip_size         = our defined pip size for the instrument

    Returns None if MT5 is unavailable or symbol not found.
    """
    if not MT5_AVAILABLE:
        return None

    info = mt5.symbol_info(symbol)
    if info is None:
        # Try selecting the symbol first
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)

    if info is None:
        logger.warning(f"MT5 symbol info not found for {symbol}")
        return None

    tick_size  = info.trade_tick_size
    tick_value = info.trade_tick_value  # already in account currency per 1 lot
    ps         = pip_size(symbol)

    if tick_size <= 0:
        logger.warning(f"tick_size is 0 for {symbol} — cannot compute pip value")
        return None

    pip_val = (ps / tick_size) * tick_value

    logger.debug(
        f"[MT5 pip value] {symbol}: tick_size={tick_size} "
        f"tick_value={tick_value:.4f} pip_size={ps} → ${pip_val:.4f}/pip/lot"
    )
    return pip_val


# ── Fallback pip value (simulation / no MT5) ─────────────────────────────────

def get_pip_value_fallback(symbol: str) -> float:
    """
    Approximate pip value used only when MT5 is not available.
    Uses hardcoded rates — acceptable for development/testing, not for live trading.
    """
    clean  = _strip_suffix(symbol).upper()
    ps     = pip_size(clean)
    units  = LOT_UNITS.get(clean, DEFAULT_LOT_UNITS)

    # Indices / commodities
    if clean in LOT_UNITS:
        return ps * units

    # USD is quote currency (EURUSD, GBPUSD, XAUUSD…) → direct
    if clean.endswith("USD"):
        return ps * units

    # USD is base currency (USDJPY, USDCHF…) → divide by approximate rate
    if clean.startswith("USD"):
        approx_rates = {
            "JPY": 150.0, "CHF": 0.90, "CAD": 1.36,
            "SEK": 10.5,  "NOK": 10.5, "DKK": 6.9,
        }
        quote = clean[3:]
        rate  = approx_rates.get(quote, 1.0)
        return (ps * units) / rate

    # Cross pairs — rough approximation
    return ps * units


# ── Core position size formula ────────────────────────────────────────────────

def calculate_lot_size(
    balance: float,
    risk_percent: float,
    stop_loss_pips: float,
    symbol: str,
) -> float:
    """
    Standard position sizing formula:

        risk_amount  = balance × (risk_percent / 100)
        lot_size     = risk_amount / (stop_loss_pips × pip_value_per_lot)

    Tries to get pip_value_per_lot from MT5 live data first.
    Falls back to approximation if MT5 unavailable.

    Returns lot size rounded DOWN to nearest 0.01 lot.
    Returns 0.0 on invalid inputs.
    """
    if balance <= 0 or risk_percent <= 0 or stop_loss_pips <= 0:
        logger.warning(
            f"Invalid inputs: balance={balance}, risk={risk_percent}%, "
            f"sl_pips={stop_loss_pips}"
        )
        return 0.0

    # Get pip value — live MT5 first, fallback second
    pip_val = get_pip_value_from_mt5(symbol)
    source  = "MT5 live"

    if pip_val is None:
        pip_val = get_pip_value_fallback(symbol)
        source  = "fallback approximation"
        logger.warning(
            f"Using {source} for pip value of {symbol} — "
            "results may be slightly inaccurate"
        )

    if pip_val <= 0:
        logger.error(f"Pip value is 0 for {symbol} — cannot size position")
        return 0.0

    risk_amount = balance * (risk_percent / 100.0)
    raw         = risk_amount / (stop_loss_pips * pip_val)

    # Round DOWN — never exceed the intended risk
    lot = math.floor(raw * 100) / 100.0

    logger.info(
        f"[LOT CALC | {source}] {symbol} | "
        f"balance=${balance:.2f} | risk={risk_percent}% (${risk_amount:.2f}) | "
        f"SL={stop_loss_pips} pips | pip_value=${pip_val:.4f}/lot → {lot} lots"
    )
    return lot


# ── Public entry point ────────────────────────────────────────────────────────

async def get_lot_size(
    balance: float,
    risk_percent: float,
    entry_price: float,
    stop_loss_price: float,
    symbol: str,
    account_currency: str = "USD",
) -> float:
    """
    Main entry point called by the pipeline.

    1. Converts entry/SL prices to pips using instrument-aware pip size.
    2. Fetches live pip value from MT5 (falls back to approximation if needed).
    3. Returns the calculated lot size rounded down to 0.01.
    """
    sl_pips = price_to_pips(symbol, abs(entry_price - stop_loss_price))
    logger.info(
        f"SL distance for {symbol}: "
        f"|{entry_price} - {stop_loss_price}| = "
        f"{abs(entry_price - stop_loss_price):.5f} → {sl_pips} pips"
    )

    return calculate_lot_size(balance, risk_percent, sl_pips, symbol)