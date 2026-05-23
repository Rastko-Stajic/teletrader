"""
TeleTrader - Telegram -> MT5 Signal Bot
"""

import asyncio
import threading
from datetime import datetime, timezone
import uvicorn
from core.telegram_listener import TelegramListener
from core.signal_parser import SignalParser
from core.signal import Signal, CloseSignal, CloseType, MoveSLSignal, OrderType
from core.mt5_executor import MT5Executor
from core.multi_account_executor import MultiAccountExecutor, build_executors
from core.risk_manager import RiskManager
from core.lot_calculator import get_lot_size
from core.position_tracker import PositionTracker
from core.logger import get_logger, log_unrecognized
from ui.dashboard import app as dashboard_app, _state as dashboard_state, push_signal, push_trade
from config.settings import Settings

logger = get_logger("main")

# Global references so dashboard endpoints can reach runtime objects
_settings: Settings = None
_risk: RiskManager = None


async def run_bot():
    global _settings, _risk

    settings = Settings()
    _settings = settings

    errors = settings.validate()
    if errors:
        for e in errors:
            logger.error(f"Config error: {e}")
        return

    parser   = SignalParser()
    risk     = RiskManager(settings)
    _risk    = risk
    executors = build_executors(settings)
    executor  = MultiAccountExecutor(executors)
    tracker   = PositionTracker()

    # Sync initial gold state to dashboard
    dashboard_state["gold_enabled"] = settings.gold_enabled

    if not executor.connect_all():
        logger.error("Failed to connect to any MT5 account. Make sure MetaTrader5 is running.")
        return

    logger.info(
        f"Connected to {len(executor._active())} MT5 account(s): "
        f"{[ex.label for ex in executor._active()]}"
    )

    async def on_signal(signal: Signal):
        await handle_open(signal, risk, executor, tracker, settings)

    async def on_close(close_signal: CloseSignal):
        await handle_close(close_signal, executor, tracker)

    async def on_move_sl(move_signal: MoveSLSignal):
        await handle_move_sl(move_signal, executor, tracker)

    def on_unrecognized(text: str, message_id: int):
        log_unrecognized(text, message_id)

    listener = TelegramListener(
        settings=settings,
        on_signal=on_signal,
        on_close=on_close,
        on_move_sl=on_move_sl,
        on_unrecognized=on_unrecognized,
        parser=parser,
    )

    logger.info("TeleTrader started. Listening for signals...")
    await listener.start()


# ── Open position pipeline ────────────────────────────────────────────────────

async def handle_open(
    signal: Signal,
    risk: RiskManager,
    executor: MultiAccountExecutor,
    tracker: PositionTracker,
    settings: Settings,
):
    if signal is None:
        return

    logger.info(f"Open signal: {signal}")

    push_signal({
        "symbol":       signal.symbol,
        "direction":    signal.direction.value,
        "entry_price":  signal.entry_price,
        "stop_loss":    signal.stop_loss,
        "take_profits": signal.take_profits,
        "risk_percent": signal.risk_percent,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    })

    # Step 1 — Risk approval (includes gold toggle check)
    # Use primary account for the approval check (symbol, direction, etc.)
    if not signal.stop_loss or not signal.risk_percent:
        missing = [f for f, v in [
            ("risk %",    signal.risk_percent),
            ("stop loss", signal.stop_loss),
        ] if not v]
        logger.warning(f"Missing {', '.join(missing)} — using default lot: {settings.default_lot_size}")
        signal.lot_size = settings.default_lot_size

    approved, reason = risk.approve(signal)
    if not approved:
        logger.warning(f"Signal BLOCKED: {reason}")
        return

    # Step 2 — Execute on all accounts (each calculates its own lot size)
    if signal.stop_loss and signal.risk_percent:
        results = await executor.execute_all(signal, signal.risk_percent)
    else:
        # No risk % or SL — use default lot, execute directly
        signal.order_type = OrderType.MARKET
        results = []
        for ex in executor._active():
            live_price = ex.get_live_price(signal.symbol, signal.direction.value)
            if live_price:
                signal.entry_price = live_price
            result = ex.execute(signal)
            results.append({"label": ex.label, **result})

    # Step 3 — Record successful trades in tracker
    for result in results:
        if result.get("success") and signal.source_message_id:
            tracker.record_open(
                telegram_message_id=signal.source_message_id,
                mt5_ticket=result["ticket"],
                symbol=result.get("symbol", signal.symbol),
                direction=signal.direction.value,
                lot=result.get("lot", signal.lot_size),
                entry_price=result.get("price", 0),
            )
            logger.info(f"[{result.get('label', '?')}] Trade OPENED: ticket={result.get('ticket')} "
                       f"{signal.direction.value} {result.get('lot')} @ {result.get('price')}")
        elif not result.get("success"):
            logger.error(f"[{result.get('label', '?')}] Trade FAILED: {result.get('error')}")


# ── Telegram message fetch helper ────────────────────────────────────────────────

async def _fetch_symbol_from_tg_message(close_signal, message_id: int):
    """
    Fetch a specific Telegram message by ID and try to parse a symbol from it.
    Uses the client reference attached to close_signal by TelegramListener.
    Returns symbol string or None.
    """
    client   = getattr(close_signal, "_tg_client",   None)
    group_id = getattr(close_signal, "_tg_group_id", None)

    if not client or not group_id:
        logger.debug("No Telegram client context available for message fetch")
        return None

    try:
        messages = await client.get_messages(group_id, ids=message_id)
        if not messages:
            return None
        msg = messages if not isinstance(messages, list) else messages[0]
        if not msg or not msg.text:
            return None

        from core.signal_parser import SignalParser
        sp = SignalParser()
        symbol = sp._extract_symbol(msg.text)
        if symbol:
            logger.info(
                f"Symbol '{symbol}' extracted from Telegram message [{message_id}]: "
                f"{msg.text[:60]}..."
            )
        return symbol
    except Exception as e:
        logger.warning(f"Failed to fetch Telegram message [{message_id}]: {e}")
        return None


# ── Close position pipeline ───────────────────────────────────────────────────

async def handle_close(
    close_signal: CloseSignal,
    executor: MultiAccountExecutor,
    tracker: PositionTracker,
):
    from ui.dashboard import push_error
    logger.info(f"Close signal: {close_signal}")

    # ── CLOSE ALL ─────────────────────────────────────────────────────────────
    if close_signal.close_type == CloseType.CLOSE_ALL:
        result = executor.close_all_positions()
        if result["success"]:
            logger.info(f"All positions closed: tickets={result['closed']}")
            for record in tracker.all_open_records():
                tracker.record_close(record["telegram_message_id"])
        else:
            logger.error(f"Close-all partial failure: {result['failed']}")
        return

    # ── CLOSE ─────────────────────────────────────────────────────────────────
    if close_signal.close_type == CloseType.CLOSE:
        ref_id = close_signal.reply_to_message_id

        # Reply to a specific open signal → close all positions with same
        # symbol AND direction as the replied-to signal.
        if ref_id:
            record = tracker.get_record(ref_id)
            if not record:
                msg = (
                    f"No tracked position found for reply_to_msg_id={ref_id}. "
                    "It may already be closed or was opened before the bot started."
                )
                logger.error(msg)
                push_error(msg)
                return

            symbol    = record["symbol"]
            direction = record["direction"]  # "BUY" or "SELL"

            result = executor.close_positions_by_symbol_and_direction(symbol, direction)
            if result.get("closed"):
                logger.info(f"Closed all {direction} {symbol} positions: tickets={result['closed']}")
                for rec in tracker.all_open_records():
                    if (
                        rec.get("symbol") == symbol
                        and rec.get("direction") == direction
                        and rec["mt5_ticket"] in result["closed"]
                    ):
                        tracker.record_close(
                            rec["telegram_message_id"],
                            close_price=close_signal.close_price,
                            realized_pips=close_signal.realized_pips,
                        )
            if result.get("failed"):
                logger.error(f"Some {direction} {symbol} closes failed: {result['failed']}")
                push_error(f"Failed to close some {direction} {symbol} positions: {result['failed']}")
            if not result.get("closed") and not result.get("simulated"):
                msg = result.get("error", f"No open {direction} {symbol} positions found to close")
                logger.error(msg)
                push_error(msg)
            return

        # No reply context → close all positions for the named symbol
        symbol = close_signal.symbol

        # ── Level 1: symbol explicitly in close message ───────────────────────
        # (already in close_signal.symbol — nothing to do)

        # ── Level 2: fetch replied-to message from Telegram and parse it ──────
        if not symbol and close_signal.reply_to_message_id:
            # First try the tracker (instant, no network)
            record = tracker.get_record(close_signal.reply_to_message_id)
            if record:
                symbol = record.get("symbol")
                logger.info(f"Symbol resolved from tracker reply: {symbol}")
            else:
                # Tracker doesn't have it — fetch the actual Telegram message
                symbol = await _fetch_symbol_from_tg_message(
                    close_signal,
                    close_signal.reply_to_message_id,
                )

        # ── Level 3: fetch the previous Telegram message and parse it ─────────
        if not symbol:
            tg_msg_id = getattr(close_signal, "_tg_message_id", None)
            if tg_msg_id:
                symbol = await _fetch_symbol_from_tg_message(
                    close_signal,
                    tg_msg_id - 1,
                )
                if symbol:
                    logger.info(f"Symbol resolved from previous Telegram message: {symbol}")

        # ── Level 4: ask MT5 for the most recently opened position ────────────
        if not symbol:
            positions = executor.get_open_positions()
            if positions:
                # MT5 returns positions; sort by ticket descending (highest = most recent)
                latest = sorted(positions, key=lambda p: p["ticket"], reverse=True)[0]
                symbol = latest["symbol"]
                # Strip broker suffix for consistency (e.g. GBPUSD.bp → GBPUSD)
                if "." in symbol:
                    symbol = symbol.split(".")[0]
                logger.warning(
                    f"MT5 fallback: closing most recently opened position "
                    f"{latest['type']} {symbol} ticket={latest['ticket']} "
                    f"(could not determine symbol from message context)"
                )

        if not symbol:
            msg = (
                "Close signal received but could not determine the symbol. "
                "No open positions found in tracker or MT5 to fall back to."
            )
            logger.error(msg)
            push_error(msg)
            return

        result = executor.close_positions_by_symbol(symbol)
        if result.get("closed"):
            logger.info(f"Closed all {symbol} positions: tickets={result['closed']}")
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

    # ── CANCEL pending order ──────────────────────────────────────────────────
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


# ── Move SL pipeline ──────────────────────────────────────────────────────────

async def handle_move_sl(
    move_signal: MoveSLSignal,
    executor: MultiAccountExecutor,
    tracker: PositionTracker,
):
    from ui.dashboard import push_error
    logger.info(f"Move SL signal: {move_signal}")

    symbol    = move_signal.symbol
    direction = move_signal.direction

    # Try to resolve symbol+direction from reply reference
    if move_signal.reply_to_message_id:
        record = tracker.get_record(move_signal.reply_to_message_id)
        if record:
            symbol    = symbol    or record.get("symbol")
            direction = direction or record.get("direction")

    if not symbol:
        msg = (
            "Move SL signal received but could not determine the symbol. "
            "Include the symbol in the message or send as a reply to the "
            "original open signal."
        )
        logger.error(msg)
        push_error(msg)
        return

    if not direction:
        msg = (
            f"Move SL signal for {symbol} received but could not determine "
            "direction (BUY/SELL). Include it in the message or send as a "
            "reply to the original open signal."
        )
        logger.error(msg)
        push_error(msg)
        return

    result = executor.modify_sl_by_symbol_and_direction(
        symbol=symbol,
        direction=direction,
        new_sl=move_signal.new_sl,
    )

    if result.get("modified"):
        logger.info(
            f"SL moved to {move_signal.new_sl} for {direction} {symbol}: "
            f"tickets={result['modified']}"
        )
    if result.get("failed"):
        logger.error(f"SL modify failed for some positions: {result['failed']}")
        push_error(
            f"Failed to move SL for some {direction} {symbol} positions: "
            f"{result['failed']}"
        )
    if not result.get("modified") and not result.get("simulated"):
        msg = result.get("error", f"No matching {direction} {symbol} positions found")
        logger.error(msg)
        push_error(msg)


# ── Dashboard gold toggle endpoint (wired to runtime risk manager) ────────────

@dashboard_app.post("/api/gold/toggle")
async def toggle_gold_endpoint():
    if _risk is None:
        return {"gold_enabled": False, "message": "Bot not running yet"}
    enabled = _risk.toggle_gold()
    dashboard_state["gold_enabled"] = enabled
    state = "enabled" if enabled else "disabled"
    return {"gold_enabled": enabled, "message": f"Gold trading {state}"}


# ── Dashboard & entry point ───────────────────────────────────────────────────

def run_dashboard(host: str, port: int):
    uvicorn.run(dashboard_app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    _settings = Settings()
    dashboard_thread = threading.Thread(
        target=run_dashboard,
        args=(_settings.dashboard_host, _settings.dashboard_port),
        daemon=True,
    )
    dashboard_thread.start()
    logger.info(f"Dashboard running at http://{_settings.dashboard_host}:{_settings.dashboard_port}")
    asyncio.run(run_bot())