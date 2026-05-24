"""
TeleTrader - Signal Service

Responsibilities:
  - Listens to the Telegram group
  - Parses all signal types (open, close, move SL, unrecognized)
  - Broadcasts parsed signals via ZeroMQ to all connected trader processes
  - Hosts the main dashboard

Start with:
    python signal_service.py
"""

import asyncio
import threading
import json
import zmq
import uvicorn
from datetime import datetime, timezone

from core.telegram_listener import TelegramListener
from core.signal_parser import SignalParser
from core.signal import Signal, CloseSignal, CloseType, MoveSLSignal
from core.logger import get_logger, log_unrecognized
from ui.dashboard import app as dashboard_app, _state as dashboard_state, push_signal
from config.settings import load_shared_settings

logger = get_logger("signal_service")


def signal_to_dict(signal) -> dict:
    """Serialize any signal type to a JSON-safe dict with a 'type' field."""
    if isinstance(signal, Signal):
        return {
            "type": "OPEN",
            "direction": signal.direction.value,
            "symbol": signal.symbol,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profits": signal.take_profits,
            "risk_percent": signal.risk_percent,
            "order_type": signal.order_type.value,
            "raw_message": signal.raw_message,
            "source_message_id": signal.source_message_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    elif isinstance(signal, CloseSignal):
        return {
            "type": "CLOSE",
            "close_type": signal.close_type.value,
            "symbol": signal.symbol,
            "reply_to_message_id": signal.reply_to_message_id,
            "close_price": signal.close_price,
            "realized_pips": signal.realized_pips,
            "raw_message": signal.raw_message,
            "source_message_id": signal.source_message_id,
            # Attach Telegram context for symbol resolution fallbacks
            "_tg_group_id": getattr(signal, "_tg_group_id", None),
            "_tg_message_id": getattr(signal, "_tg_message_id", None),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    elif isinstance(signal, MoveSLSignal):
        return {
            "type": "MOVE_SL",
            "new_sl": signal.new_sl,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "reply_to_message_id": signal.reply_to_message_id,
            "raw_message": signal.raw_message,
            "source_message_id": signal.source_message_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    return {}


class SignalBroadcaster:
    """Publishes serialized signals to all trader subscribers via ZeroMQ PUB socket."""

    def __init__(self, host: str, port: int):
        self.context = zmq.Context()
        self.socket  = self.context.socket(zmq.PUB)
        self.socket.bind(f"tcp://{host}:{port}")
        logger.info(f"ZeroMQ publisher bound to tcp://{host}:{port}")

    def broadcast(self, signal_dict: dict):
        payload = json.dumps(signal_dict)
        self.socket.send_string(payload)
        logger.debug(f"Broadcasted: {signal_dict.get('type')} {signal_dict.get('symbol', '')}")

    def close(self):
        self.socket.close()
        self.context.term()


async def run_signal_service():
    settings = load_shared_settings()

    errors = settings.validate()
    if errors:
        for e in errors:
            logger.error(f"Config error: {e}")
        return

    broadcaster = SignalBroadcaster(settings.zmq_host, settings.zmq_port)
    parser      = SignalParser()

    async def on_signal(signal: Signal):
        logger.info(f"Open signal: {signal}")
        d = signal_to_dict(signal)
        push_signal({
            "symbol":       signal.symbol,
            "direction":    signal.direction.value,
            "entry_price":  signal.entry_price,
            "stop_loss":    signal.stop_loss,
            "take_profits": signal.take_profits,
            "risk_percent": signal.risk_percent,
            "timestamp":    d["timestamp"],
        })
        broadcaster.broadcast(d)

    async def on_close(close_signal: CloseSignal):
        logger.info(f"Close signal: {close_signal}")
        broadcaster.broadcast(signal_to_dict(close_signal))

    async def on_move_sl(move_signal: MoveSLSignal):
        logger.info(f"Move SL signal: {move_signal}")
        broadcaster.broadcast(signal_to_dict(move_signal))

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

    logger.info("Signal Service started. Listening for Telegram signals...")
    await listener.start()


def run_dashboard(host: str, port: int):
    uvicorn.run(dashboard_app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    settings = load_shared_settings()

    dashboard_thread = threading.Thread(
        target=run_dashboard,
        args=(settings.dashboard_host, settings.dashboard_port),
        daemon=True,
    )
    dashboard_thread.start()
    logger.info(f"Dashboard at http://{settings.dashboard_host}:{settings.dashboard_port}")

    asyncio.run(run_signal_service())
