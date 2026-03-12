"""
TeleTrader - Telegram -> MT5 Signal Bot
"""

import asyncio
import threading
import uvicorn
from core.telegram_listener import TelegramListener
from core.signal_parser import SignalParser
from core.signal import Signal, CloseSignal, CloseType
from core.mt5_executor import MT5Executor
from core.risk_manager import RiskManager
from core.lot_calculator import get_lot_size
from core.position_tracker import PositionTracker
from core.logger import log_unrecognized
from core.logger import get_logger
from ui.dashboard import app as dashboard_app
from config.settings import Settings

logger = get_logger("main")


async def run_bot():
    settings = Settings()

    errors = settings.validate()
    if errors:
        for e in errors:
            logger.error(f"Config error: {e}")
        return

    parser  = SignalParser()
    risk    = RiskManager(settings)
    executor = MT5Executor(settings)
    tracker  = PositionTracker()

    if not executor.connect():
        logger.error("Failed to connect to MT5. Make sure MetaTrader5 is running.")
        return

    async def on_signal(signal: Signal):
        await handle_open(signal, risk, executor, tracker, settings)

    async def on_close(close_signal: CloseSignal):
        await handle_close(close_signal, executor, tracker)

    def on_unrecognized(text: str, message_id: int):
        log_unrecognized(text, message_id)

    listener = TelegramListener(
        settings=settings,
        on_signal=on_signal,
        on_close=on_close,
        on_unrecognized=on_unrecognized,
        parser=parser,
    )

    logger.info("TeleTrader started. Listening for signals...")
    await listener.start()


# ── Open position pipeline ────────────────────────────────────────────────────

async def handle_open(
    signal: Signal,
    risk: RiskManager,
    executor: MT5Executor,
    tracker: PositionTracker,
    settings: Settings,
):
    if signal is None:
        return

    logger.info(f"Open signal: {signal}")

    # Step 1 — Calculate lot size
    if signal.entry_price and signal.stop_loss and signal.risk_percent:
        account = executor.get_account_info()
        balance = account.get("balance", 0)
        currency = account.get("currency", "USD")

        if balance <= 0:
            logger.error("Cannot size position: MT5 balance unavailable")
            return

        lot = await get_lot_size(
            balance=balance,
            risk_percent=signal.risk_percent,
            entry_price=signal.entry_price,
            stop_loss_price=signal.stop_loss,
            symbol=signal.symbol,
            account_currency=currency,
        )
        if lot <= 0:
            logger.error(f"Lot size {lot} invalid — aborting")
            return
        signal.lot_size = lot
    else:
        missing = [f for f, v in [
            ("risk %",      signal.risk_percent),
            ("entry price", signal.entry_price),
            ("stop loss",   signal.stop_loss),
        ] if not v]
        logger.warning(f"Missing {', '.join(missing)} — using default lot: {settings.default_lot_size}")
        signal.lot_size = settings.default_lot_size

    # Step 2 — Risk approval
    approved, reason = risk.approve(signal)
    if not approved:
        logger.warning(f"Signal BLOCKED: {reason}")
        return

    # Step 3 — Execute
    result = executor.execute(signal)
    if result["success"] and signal.source_message_id:
        tracker.record_open(
            telegram_message_id=signal.source_message_id,
            mt5_ticket=result["ticket"],
            symbol=signal.symbol,
            direction=signal.direction.value,
            lot=result["lot"],
            entry_price=result["price"],
        )
        logger.info(f"Trade OPENED: {result}")
    elif not result["success"]:
        logger.error(f"Trade FAILED: {result.get('error')}")


# ── Close position pipeline ───────────────────────────────────────────────────

async def handle_close(
    close_signal: CloseSignal,
    executor: MT5Executor,
    tracker: PositionTracker,
):
    from ui.dashboard import push_error
    logger.info(f"Close signal: {close_signal}")

    # ── CLOSE ALL (every open position, all symbols) ──────────────────────────
    if close_signal.close_type == CloseType.CLOSE_ALL:
        result = executor.close_all_positions()
        if result["success"]:
            logger.info(f"All positions closed: tickets={result['closed']}")
            for record in tracker.all_open_records():
                tracker.record_close(record["telegram_message_id"])
        else:
            logger.error(f"Close-all partial failure: {result['failed']}")
        return

    # ── CLOSE (all positions for the symbol) ──────────────────────────────────
    if close_signal.close_type == CloseType.CLOSE:
        symbol = close_signal.symbol

        if not symbol:
            # Symbol not in close message — try to resolve via reply reference
            ref_id = close_signal.reply_to_message_id
            if ref_id:
                record = tracker.get_record(ref_id)
                if record:
                    symbol = record.get("symbol")

        if not symbol:
            msg = (
                "Close signal received but could not determine the symbol. "
                "Include the symbol in the message (e.g. 'Close XAUUSD') "
                "or send it as a reply to the original open signal."
            )
            logger.error(msg)
            push_error(msg)
            return

        result = executor.close_positions_by_symbol(symbol)
        if result.get("closed"):
            logger.info(f"Closed all {symbol} positions: tickets={result['closed']}")
            # Mark matching tracker records as closed
            for record in tracker.all_open_records():
                if record.get("symbol") == symbol and record["mt5_ticket"] in result["closed"]:
                    tracker.record_close(
                        record["telegram_message_id"],
                        close_price=close_signal.close_price,
                        realized_pips=close_signal.realized_pips,
                    )
        if result.get("failed"):
            logger.error(f"Some {symbol} closes failed: {result['failed']}")
            push_error(f"Failed to close some {symbol} positions: {result['failed']}")
        if not result.get("closed") and not result.get("simulated"):
            msg = result.get("error", f"No open {symbol} positions found to close")
            logger.error(msg)
            push_error(msg)
        return

    # ── CANCEL pending order (still reply-linked — need the specific ticket) ───
    if close_signal.close_type == CloseType.CANCEL:
        ref_id = close_signal.reply_to_message_id
        if not ref_id:
            msg = "Cancel signal has no reply reference — cannot identify which order to cancel."
            logger.error(msg)
            push_error(msg)
            return

        ticket = tracker.get_ticket(ref_id)
        if ticket is None:
            msg = (
                f"No pending order found for reply_to_msg_id={ref_id}. "
                "It may already be cancelled or filled."
            )
            logger.error(msg)
            push_error(msg)
            return

        result = executor.cancel_pending_order(ticket)
        if result["success"]:
            tracker.record_close(ref_id)
            logger.info(f"Pending order CANCELLED: ticket={ticket}")
        else:
            logger.error(f"Cancel FAILED: ticket={ticket} error={result.get('error')}")
            push_error(f"Failed to cancel order {ticket}: {result.get('error')}")


# ── Dashboard & entry point ───────────────────────────────────────────────────

def run_dashboard():
    uvicorn.run(dashboard_app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
    dashboard_thread.start()
    logger.info("Dashboard running at http://127.0.0.1:8000")
    asyncio.run(run_bot())
