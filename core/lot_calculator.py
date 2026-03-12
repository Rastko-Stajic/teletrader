"""
LotCalculator - computes position size (standard lots) from:
    - account balance (from MT5)
    - risk % (from Telegram signal)
    - stop loss distance in pips (derived from signal entry vs SL prices)

Strategy:
    PRIMARY  → local Python math (instant, no network)
    FALLBACK → Playwright headless browser fills https://boris-fx.com/forex-calculator/
               and reads back the "Standard Lots" result

Pip conventions implemented:
    - Standard Forex (non-JPY quote):  1 pip = 0.0001
    - JPY pairs (e.g. USDJPY):         1 pip = 0.01
    - XAUUSD (Gold):                   1 pip = 0.10
    - Indices (NAS100, US30, SPX500):  1 pip = 1.0  (1 full point)
"""

from __future__ import annotations

import math
from typing import Optional
from core.logger import get_logger

logger = get_logger("calculator")

# ── Pip size lookup ───────────────────────────────────────────────────────────

# Symbols whose pip size is NOT the standard 0.0001
CUSTOM_PIP_SIZE: dict[str, float] = {
    # JPY pairs
    "USDJPY": 0.01,
    "EURJPY": 0.01,
    "GBPJPY": 0.01,
    "AUDJPY": 0.01,
    "CADJPY": 0.01,
    "CHFJPY": 0.01,
    "NZDJPY": 0.01,
    "SGDJPY": 0.01,
    # Gold
    "XAUUSD": 0.10,
    # Silver – 0.001 per pip is conventional for XAGUSD
    "XAGUSD": 0.001,
    # Indices – treated as 1 point per pip
    "NAS100": 1.0,
    "US30":   1.0,
    "DJ30":   1.0,
    "SPX500": 1.0,
    "GER40":  1.0,
    "UK100":  1.0,
}

# Standard lot sizes (units of base currency / commodity)
# For Forex: 1 standard lot = 100,000 units
# For Gold:  1 standard lot = 100 oz
# For most indices: 1 standard lot = 1 contract (value varies by broker)
LOT_UNITS: dict[str, float] = {
    "XAUUSD": 100.0,    # 100 troy oz per standard lot
    "XAGUSD": 5000.0,
    "NAS100": 1.0,
    "US30":   1.0,
    "DJ30":   1.0,
    "SPX500": 1.0,
}
DEFAULT_LOT_UNITS = 100_000.0  # standard Forex lot


# ── Pip value calculation ─────────────────────────────────────────────────────

def pip_size(symbol: str) -> float:
    """Return the monetary value of 1 pip movement for the given symbol."""
    return CUSTOM_PIP_SIZE.get(symbol.upper(), 0.0001)


def price_to_pips(symbol: str, price_distance: float) -> float:
    """Convert a raw price difference to pips for the given symbol."""
    ps = pip_size(symbol)
    return round(abs(price_distance) / ps, 1)


def pip_value_per_lot(symbol: str, account_currency: str = "USD") -> float:
    """
    Return the USD value of 1 pip movement per 1 standard lot.

    Assumptions (covers most retail broker setups with USD account):
      - USD-quoted pairs (EURUSD, GBPUSD, XAUUSD): pip value = pip_size × lot_units
      - USD-base pairs (USDJPY, USDCHF):            pip value ≈ pip_size × lot_units / current_price
        → We approximate with a fixed ratio; for live precision use MT5 tick values.
      - Indices:                                     pip value = pip_size × contract_size (broker-specific)

    For production, prefer reading pip value directly from MT5:
        mt5.symbol_info(symbol).trade_tick_value
    """
    sym = symbol.upper()
    ps = pip_size(sym)
    units = LOT_UNITS.get(sym, DEFAULT_LOT_UNITS)

    # Indices / commodities with custom lot sizes
    if sym in LOT_UNITS:
        return ps * units

    # USD is the quote currency (EURUSD, GBPUSD, etc.) -> direct
    if sym.endswith("USD"):
        return ps * units  # e.g. 0.0001 x 100000 = $10/pip

    # USD is the base currency (USDJPY, USDCHF, USDCAD, etc.)
    # pip value in quote ccy = pip_size * lot_units; convert to USD
    if sym.startswith("USD"):
        approx_rates = {
            "JPY": 150.0, "CHF": 0.90, "CAD": 1.36,
            "SEK": 10.5, "NOK": 10.5, "DKK": 6.9,
        }
        quote = sym[3:]
        rate = approx_rates.get(quote, 1.0)
        return (ps * units) / rate  # e.g. USDJPY: (0.01 x 100000) / 150 = $6.67/pip

    # Cross pairs (EURGBP, EURJPY, GBPJPY, etc.) - approximate
    # For precision use mt5.symbol_info(symbol).trade_tick_value
    return ps * units


# ── Core position size formula ────────────────────────────────────────────────

def calculate_lot_size(
    balance: float,
    risk_percent: float,
    stop_loss_pips: float,
    symbol: str,
    account_currency: str = "USD",
) -> float:
    """
    Standard position sizing formula:

        risk_amount  = balance × (risk_percent / 100)
        lot_size     = risk_amount / (stop_loss_pips × pip_value_per_lot)

    Returns lot size rounded to 2 decimal places (standard broker precision).
    Returns 0.0 on invalid inputs.
    """
    if balance <= 0 or risk_percent <= 0 or stop_loss_pips <= 0:
        logger.warning(
            f"Invalid inputs for lot calc: balance={balance}, "
            f"risk={risk_percent}%, sl_pips={stop_loss_pips}"
        )
        return 0.0

    risk_amount = balance * (risk_percent / 100.0)
    pv = pip_value_per_lot(symbol, account_currency)

    if pv <= 0:
        logger.error(f"Pip value is zero for {symbol} — cannot size position")
        return 0.0

    raw = risk_amount / (stop_loss_pips * pv)

    # Round DOWN to nearest 0.01 lot (never round up — that would exceed risk)
    lot = math.floor(raw * 100) / 100.0

    logger.info(
        f"[LOCAL CALC] {symbol} | balance={balance} | risk={risk_percent}% "
        f"(${risk_amount:.2f}) | SL={stop_loss_pips} pips | "
        f"pip_value=${pv:.4f}/lot → {lot} lots"
    )
    return lot


# ── Browser fallback via Playwright ──────────────────────────────────────────

# Maps our internal symbol names to the calculator's dropdown option text
CALCULATOR_PAIR_MAP: dict[str, str] = {
    "EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDCHF": "USD/CHF",
    "USDCAD": "USD/CAD", "USDJPY": "USD/JPY", "NZDUSD": "NZD/USD",
    "AUDUSD": "AUD/USD", "EURAUD": "EUR/AUD", "EURGBP": "EUR/GBP",
    "EURJPY": "EUR/JPY", "EURCAD": "EUR/CAD", "EURCHF": "EUR/CHF",
    "EURNZD": "EUR/NZD", "GBPCAD": "GBP/CAD", "GBPCHF": "GBP/CHF",
    "GBPJPY": "GBP/JPY", "GBPAUD": "GBP/AUD", "GBPNZD": "GBP/NZD",
    "AUDCAD": "AUD/CAD", "AUDJPY": "AUD/JPY", "AUDCHF": "AUD/CHF",
    "AUDNZD": "AUD/NZD", "CHFJPY": "CHF/JPY", "CADCHF": "CAD/CHF",
    "CADJPY": "CAD/JPY", "NZDCHF": "NZD/CHF", "NZDJPY": "NZD/JPY",
    "NZDCAD": "NZD/CAD",
}

CALCULATOR_URL = "https://boris-fx.com/forex-calculator/"


async def calculate_lot_size_browser(
    balance: float,
    risk_percent: float,
    stop_loss_pips: float,
    symbol: str,
) -> Optional[float]:
    """
    Fills the web calculator at CALCULATOR_URL and reads back Standard Lots.
    Returns None if symbol not supported by the calculator or on any error.
    Requires: pip install playwright && playwright install chromium
    """
    pair_label = CALCULATOR_PAIR_MAP.get(symbol.upper())
    if not pair_label:
        logger.info(
            f"[BROWSER CALC] {symbol} not in calculator dropdown — "
            "browser fallback not applicable, using local result only"
        )
        return None

    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        logger.warning(
            "[BROWSER CALC] Playwright not installed. "
            "Run: pip install playwright && playwright install chromium"
        )
        return None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()

            await page.goto(CALCULATOR_URL, wait_until="networkidle", timeout=15_000)

            # ── Fill inputs ───────────────────────────────────────────────────
            await page.fill("input#balance", str(balance))
            await page.fill("input#risk", str(risk_percent))
            await page.fill("input#stop-loss", str(round(stop_loss_pips, 1)))

            # Select currency pair dropdown
            await page.select_option("select", label=pair_label)

            # Click Calculate button
            await page.click("button[type='submit'], button:has-text('Calculate')")

            # ── Read Standard Lots output ─────────────────────────────────────
            # Find the <dt> that contains "Standard Lots", then get its sibling <dd>
            result_text = await page.evaluate("""
                () => {
                    const dts = document.querySelectorAll(
                        '.Calculator-module__results___PejWF dt'
                    );
                    for (const dt of dts) {
                        if (dt.textContent.trim().toLowerCase().includes('standard lots')) {
                            const dd = dt.nextElementSibling;
                            return dd ? dd.textContent.trim() : null;
                        }
                    }
                    return null;
                }
            """)

            await browser.close()

            if result_text is None:
                logger.warning("[BROWSER CALC] Could not find Standard Lots output")
                return None

            lot = float(result_text.replace(",", "."))
            logger.info(
                f"[BROWSER CALC] {symbol} | balance={balance} | "
                f"risk={risk_percent}% | SL={stop_loss_pips} pips → {lot} lots"
            )
            return lot

    except Exception as e:
        logger.error(f"[BROWSER CALC] Failed: {e}")
        return None


# ── Public interface ──────────────────────────────────────────────────────────

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

    1. Converts entry/SL prices to pips.
    2. Runs local calculation (primary).
    3. For Forex pairs supported by the web calc, also runs browser calc
       and logs comparison. Local result is always used for execution speed.
    4. Returns the local calculated lot size.
    """
    sl_pips = price_to_pips(symbol, abs(entry_price - stop_loss_price))
    logger.info(f"SL distance for {symbol}: {abs(entry_price - stop_loss_price):.5f} → {sl_pips} pips")

    # Primary: local math
    local_lot = calculate_lot_size(balance, risk_percent, sl_pips, symbol, account_currency)

    # Browser fallback disabled — Currency Pair dropdown on the calculator
    # is not yet configured correctly. Re-enable the block below once fixed.
    #
    # if symbol.upper() in CALCULATOR_PAIR_MAP:
#     browser_lot = await calculate_lot_size_browser(balance, risk_percent, sl_pips, symbol)
#     if browser_lot is not None and abs(browser_lot - local_lot) > 0.01:
#         logger.warning(f'[CALC MISMATCH] Local={local_lot} vs Browser={browser_lot}')
#     elif browser_lot is not None:
#         logger.info(f'[CALC MATCH] Local={local_lot} == Browser={browser_lot}')
    return local_lot
