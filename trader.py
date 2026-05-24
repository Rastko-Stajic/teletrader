"""
TeleTrader - Trader Process

Responsibilities:
  - Subscribes to the Signal Service via ZeroMQ
  - Executes trades on a single MT5 account
  - Manages its own position tracker and risk manager
  - Hosts its own lightweight dashboard (optional)

Start with:
    python trader.py accounts/account1.env
    python trader.py accounts/account2.env
"""

import asyncio
import sys
import json
import threading
import zmq
import zmq.asyncio
import uvicorn
from datetime import datetime, timezone

from core.mt5_executor import MT5Executor
from core.risk_manager import RiskManager
from core.lot_calculator import get_lot_size
from core.position_tracker import PositionTracker
from core.signal import Signal, CloseSignal, CloseType, MoveSLSignal, Direction, OrderType
from core.logger import get_logger
from ui.dashboard import app as dashboard_app, _state as dashboard_state, push_signal
from config.settings import load_account_settings

logger = get_logger("trader")

_settings = None
_risk     = None


def dict_to_signal(d: dict):
    """Deserialize a signal dict back into the appropriate signal object."""
    sig_type = d.get("type")

    if sig_type == "OPEN":
        s = Signal(
            direction=Direction(d["direction"]),
            symbol=d["symbol"],
            entry_price=d.get("entry_price"),
            stop_loss=d.get("stop_loss"),
            take_profits=d.get("take_profits", []),
            order_type=OrderType(d.get("order_type", "MARKET")),
            risk_percent=d.get("risk_percent"),
            raw_message=d.get("raw_message", ""),
            source_message_id=d.get("source_message_id"),
        )
        return s

    elif sig_type == "CLOSE":
        cs = CloseSignal(
            close_type=CloseType(d["close_type"]),
            symbol=d.get("symbol"),
            reply_to_message_id=d.get("reply_to_message_id"),
            close_price=d.get("close_price"),
            realized_pips=d.get("realized_pips"),
            raw_message=d.get("raw_message", ""),
            source_message_id=d.get("source_message_id"),
        )
        # Restore Telegram context for symbol resolution fallbacks
        cs._tg_group_id  = d.get("_tg_group_id")
        cs._tg_message_id = d.get("_tg_message_id")
        cs._tg_client    = None  # not available in trader process
        return cs

    elif sig_type == "MOVE_SL":
        return MoveSLSignal(
            new_sl=d["new_sl"],
            symbol=d.get("symbol"),
            direction=d.get("direction"),
            reply_to_message_id=d.get("reply_to_message_id"),
            raw_message=d.get("raw_message", ""),
            source_message_id=d.get("source_message_id"),
        )

    return None


async def handle_open(signal: Signal, executor: MT5Executor,
                      tracker: PositionTracker, settings):
    logger.info(f"[{settings.account_label}] Open signal: {signal}")

    from ui.dashboard import push_error

    # Risk approval
    if not signal.stop_loss or not signal.risk_percent:
        missing = [f for f, v in [
            ("risk %",    signal.risk_percent),
            ("stop loss", signal.stop_loss),
        ] if not v]
        logger.warning(f"Missing {', '.join(missing)} — using default lot")
        signal.lot_size   = settings.default_lot_size
        signal.order_type = OrderType.MARKET
    else:
        approved, reason = _risk.approve(signal)
        if not approved:
            logger.warning(f"Signal BLOCKED: {reason}")
            return

        # Get live price and calculate lot size for THIS account's balance
        live_price = executor.get_live_price(signal.symbol, signal.direction.value)
        if live_price is None:
            logger.error(f"Cannot get live price for {signal.symbol} — aborting")
            push_error(f"[{settings.account_label}] Cannot get live price for {signal.symbol}")
            return

        account  = executor.get_account_info()
        balance  = account.get("balance", 0)
        currency = account.get("currency", "USD")

        if balance <= 0:
            logger.error("MT5 balance unavailable")
            return

        lot = await get_lot_size(
            balance=balance,
            risk_percent=signal.risk_percent,
            entry_price=live_price,
            stop_loss_price=signal.stop_loss,
            symbol=signal.symbol,
            account_currency=currency,
        )
        if lot <= 0:
            logger.error(f"Lot size {lot} invalid — aborting")
            return

        signal.lot_size    = lot
        signal.entry_price = live_price
        signal.order_type  = OrderType.MARKET

        logger.info(
            f"[{settings.account_label}] balance=${balance:.2f} "
            f"risk={signal.risk_percent}% → lot={lot} @ {live_price}"
        )

    result = executor.execute(signal)
    if result["success"] and signal.source_message_id:
        tracker.record_open(
            telegram_message_id=signal.source_message_id,
            mt5_ticket=result["ticket"],
            symbol=result.get("symbol", signal.symbol),
            direction=signal.direction.value,
            lot=result.get("lot", signal.lot_size),
            entry_price=result.get("price", 0),
        )
        logger.info(
            f"[{settings.account_label}] Trade OPENED: "
            f"ticket={result.get('ticket')} {signal.direction.value} "
            f"{result.get('lot')} @ {result.get('price')}"
        )
    elif not result["success"]:
        logger.error(f"[{settings.account_label}] Trade FAILED: {result.get('error')}")
        push_error(f"[{settings.account_label}] Trade FAILED: {result.get('error')}")


async def handle_close(close_signal: CloseSignal, executor: MT5Executor,
                       tracker: PositionTracker, settings):
    from ui.dashboard import push_error
    logger.info(f"[{settings.account_label}] Close signal: {close_signal}")

    if close_signal.close_type == CloseType.CLOSE_ALL:
        result = executor.close_all_positions()
        if result["success"]:
            logger.info(f"All positions closed: {result['closed']}")
            for record in tracker.all_open_records():
                tracker.record_close(record["telegram_message_id"])
        else:
            logger.error(f"Close-all partial failure: {result['failed']}")
        return

    if close_signal.close_type == CloseType.CLOSE:
        ref_id = close_signal.reply_to_message_id

        if ref_id:
            record = tracker.get_record(ref_id)
            if record:
                symbol    = record["symbol"]
                direction = record["direction"]
                result = executor.close_positions_by_symbol_and_direction(symbol, direction)
                if result.get("closed"):
                    logger.info(f"Closed {direction} {symbol}: tickets={result['closed']}")
                    for rec in tracker.all_open_records():
                        if (rec.get("symbol") == symbol
                                and rec.get("direction") == direction
                                and rec["mt5_ticket"] in result["closed"]):
                            tracker.record_close(
                                rec["telegram_message_id"],
                                close_price=close_signal.close_price,
                                realized_pips=close_signal.realized_pips,
                            )
                elif not result.get("simulated"):
                    push_error(f"[{settings.account_label}] {result.get('error')}")
                return

        # No reply or not in tracker — use symbol from message or MT5 fallback
        symbol = close_signal.symbol

        if not symbol:
            positions = executor.get_open_positions()
            if positions:
                latest = sorted(positions, key=lambda p: p["ticket"], reverse=True)[0]
                symbol = latest["symbol"]
                if "." in symbol:
                    symbol = symbol.split(".")[0]
                logger.warning(
                    f"[{settings.account_label}] MT5 fallback: "
                    f"closing most recent position {symbol} ticket={latest['ticket']}"
                )

        if not symbol:
            msg = f"[{settings.account_label}] Close signal: cannot determine symbol"
            logger.error(msg)
            push_error(msg)
            return

        result = executor.close_positions_by_symbol(symbol)
        if result.get("closed"):
            logger.info(f"Closed all {symbol}: tickets={result['closed']}")
            for record in tracker.all_open_records():
                if record.get("symbol") == symbol and record["mt5_ticket"] in result["closed"]:
                    tracker.record_close(
                        record["telegram_message_id"],
                        close_price=close_signal.close_price,
                        realized_pips=close_signal.realized_pips,
                    )
        elif not result.get("simulated"):
            push_error(f"[{settings.account_label}] {result.get('error')}")
        return

    if close_signal.close_type == CloseType.CANCEL:
        ref_id = close_signal.reply_to_message_id
        if not ref_id:
            return
        ticket = tracker.get_ticket(ref_id)
        if ticket is None:
            return
        result = executor.cancel_pending_order(ticket)
        if result["success"]:
            tracker.record_close(ref_id)
            logger.info(f"Pending order CANCELLED: ticket={ticket}")
        else:
            push_error(f"[{settings.account_label}] Cancel FAILED ticket={ticket}: {result.get('error')}")


async def handle_move_sl(move_signal: MoveSLSignal, executor: MT5Executor,
                         tracker: PositionTracker, settings):
    from ui.dashboard import push_error
    logger.info(f"[{settings.account_label}] Move SL: {move_signal}")

    symbol    = move_signal.symbol
    direction = move_signal.direction

    if move_signal.reply_to_message_id:
        record = tracker.get_record(move_signal.reply_to_message_id)
        if record:
            symbol    = symbol    or record.get("symbol")
            direction = direction or record.get("direction")

    if not symbol or not direction:
        msg = f"[{settings.account_label}] Move SL: cannot determine symbol/direction"
        logger.error(msg)
        push_error(msg)
        return

    result = executor.modify_sl_by_symbol_and_direction(symbol, direction, move_signal.new_sl)
    if result.get("modified"):
        logger.info(f"SL moved to {move_signal.new_sl} for {direction} {symbol}: {result['modified']}")
    elif not result.get("simulated"):
        push_error(f"[{settings.account_label}] Move SL failed: {result.get('error')}")


async def run_trader(settings):
    global _settings, _risk
    _settings = settings
    _risk     = RiskManager(settings)

    executor = MT5Executor(
        settings=settings,
        label=settings.account_label,
        terminal_path=settings.mt5_terminal_path,
    )
    tracker = PositionTracker(
        filepath=f"logs/positions_{settings.account_label.replace(' ', '_')}.json"
    )

    dashboard_state["gold_enabled"] = settings.gold_enabled

    if not executor.connect():
        logger.error(f"[{settings.account_label}] Failed to connect to MT5")
        return

    logger.info(f"[{settings.account_label}] Connected to MT5")

    # ZeroMQ subscriber
    context = zmq.asyncio.Context()
    socket  = context.socket(zmq.SUB)
    socket.connect(f"tcp://{settings.zmq_host}:{settings.zmq_port}")
    socket.setsockopt_string(zmq.SUBSCRIBE, "")   # subscribe to all messages
    logger.info(f"[{settings.account_label}] Subscribed to signals at "
                f"tcp://{settings.zmq_host}:{settings.zmq_port}")

    logger.info(f"[{settings.account_label}] Trader ready — waiting for signals...")

    while True:
        try:
            raw = await socket.recv_string()
            d   = json.loads(raw)
            signal = dict_to_signal(d)

            if signal is None:
                logger.debug(f"Unknown signal type: {d.get('type')}")
                continue

            if isinstance(signal, Signal):
                await handle_open(signal, executor, tracker, settings)
            elif isinstance(signal, CloseSignal):
                await handle_close(signal, executor, tracker, settings)
            elif isinstance(signal, MoveSLSignal):
                await handle_move_sl(signal, executor, tracker, settings)

        except json.JSONDecodeError as e:
            logger.error(f"Invalid signal JSON: {e}")
        except Exception as e:
            logger.error(f"[{settings.account_label}] Error processing signal: {e}")


def run_dashboard(host: str, port: int):
    uvicorn.run(dashboard_app, host=host, port=port, log_level="warning")


# ── Dashboard gold toggle (per trader) ───────────────────────────────────────

@dashboard_app.post("/api/gold/toggle")
async def toggle_gold():
    if _risk is None:
        return {"gold_enabled": False, "message": "Trader not running yet"}
    enabled = _risk.toggle_gold()
    dashboard_state["gold_enabled"] = enabled
    return {"gold_enabled": enabled, "message": f"Gold {'enabled' if enabled else 'disabled'}"}


if __name__ == "__main__":
    env_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not env_path:
        print("Usage: python trader.py accounts/account1.env")
        sys.exit(1)

    settings = load_account_settings(env_path)

    errors = settings.validate()
    if errors:
        for e in errors:
            print(f"Config error: {e}")
        sys.exit(1)

    # Start per-account dashboard in background
    dashboard_thread = threading.Thread(
        target=run_dashboard,
        args=(settings.dashboard_host, settings.dashboard_port),
        daemon=True,
    )
    dashboard_thread.start()
    logger.info(
        f"[{settings.account_label}] Dashboard at "
        f"http://{settings.dashboard_host}:{settings.dashboard_port}"
    )

    asyncio.run(run_trader(settings))
