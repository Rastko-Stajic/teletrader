"""
MT5Executor - places and manages trades via the MetaTrader5 Python API.
Requires: pip install MetaTrader5
Only works on Windows with MT5 terminal installed and running.
"""

from typing import Dict, Any
from core.signal import Signal, Direction, OrderType
from core.logger import get_logger
from config.settings import Settings

logger = get_logger("mt5")

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 package not installed. Running in SIMULATION mode.")


class MT5Executor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.connected = False

    def connect(self) -> bool:
        if not MT5_AVAILABLE:
            logger.warning("MT5 not available — simulation mode active.")
            self.connected = True
            return True

        initialized = mt5.initialize(
            login=self.settings.mt5_login,
            password=self.settings.mt5_password,
            server=self.settings.mt5_server,
        )

        if not initialized:
            error = mt5.last_error()
            logger.error(f"MT5 init failed: {error}")
            return False

        account_info = mt5.account_info()
        logger.info(
            f"MT5 connected: {account_info.name} | "
            f"Balance: {account_info.balance} {account_info.currency}"
        )
        self.connected = True
        return True

    def disconnect(self):
        if MT5_AVAILABLE:
            mt5.shutdown()
        self.connected = False
        logger.info("MT5 disconnected.")

    def execute(self, signal: Signal) -> Dict[str, Any]:
        if not self.connected:
            return {"success": False, "error": "MT5 not connected"}

        if not MT5_AVAILABLE:
            return self._simulate(signal)

        lot = signal.lot_size or self.settings.default_lot_size

        # Clamp to broker limits
        mt5_symbol = self._to_mt5_symbol(signal.symbol)
        signal.symbol = mt5_symbol

        # Ensure the symbol is in Market Watch before querying it
        mt5.symbol_select(mt5_symbol, True)
        symbol_info = mt5.symbol_info(mt5_symbol)
        if symbol_info is None:
            return {"success": False, "error": f"Symbol {signal.symbol} not found in MT5"}

        lot = max(symbol_info.volume_min, min(lot, symbol_info.volume_max))

        # Determine action
        tick = mt5.symbol_info_tick(signal.symbol)
        use_market = signal.order_type == OrderType.MARKET or self.settings.force_market_execution
        if use_market:
            if signal.direction == Direction.BUY:
                action_type = mt5.ORDER_TYPE_BUY
                price = tick.ask
            else:
                action_type = mt5.ORDER_TYPE_SELL
                price = tick.bid
        else:
            # Pending order: pick LIMIT vs STOP based on entry price vs current market.
            # BUY_LIMIT  requires entry < ask  (buy cheaper than market)
            # BUY_STOP   requires entry > ask  (buy on breakout above market)
            # SELL_LIMIT requires entry > bid  (sell higher than market)
            # SELL_STOP  requires entry < bid  (sell on breakdown below market)
            price = signal.entry_price
            if signal.direction == Direction.BUY:
                action_type = mt5.ORDER_TYPE_BUY_LIMIT if price < tick.ask else mt5.ORDER_TYPE_BUY_STOP
            else:
                action_type = mt5.ORDER_TYPE_SELL_LIMIT if price > tick.bid else mt5.ORDER_TYPE_SELL_STOP

        # Normalize price to symbol's required decimal precision
        price = round(price, symbol_info.digits)

        # Resolve the filling mode the broker actually supports for this symbol.
        # filling_mode is a bitmask: 1 = FOK, 2 = IOC, 4 = RETURN
        filling_mode = symbol_info.filling_mode
        if filling_mode & 1:
            type_filling = mt5.ORDER_FILLING_FOK
        elif filling_mode & 2:
            type_filling = mt5.ORDER_FILLING_IOC
        else:
            type_filling = mt5.ORDER_FILLING_RETURN

        request = {
            "action": mt5.TRADE_ACTION_DEAL if use_market else mt5.TRADE_ACTION_PENDING,
            "symbol": signal.symbol,
            "volume": lot,
            "type": action_type,
            "price": price,
            "deviation": 20,  # max slippage in points
            "magic": 20240101,  # unique identifier for this bot's trades
            "comment": f"TeleTrader #{signal.source_message_id}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": type_filling,
        }

        if signal.stop_loss:
            request["sl"] = signal.stop_loss

        if signal.take_profits:
            request["tp"] = signal.take_profits[0]  # MT5 supports single TP per order

        result = mt5.order_send(request)

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            error_msg = f"Order failed: retcode={result.retcode}, comment={result.comment}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "retcode": result.retcode}

        logger.info(
            f"Order placed: ticket={result.order} | {signal.direction.value} "
            f"{lot} {signal.symbol} @ {price}"
        )

        # If multiple TPs, place additional partial orders
        if len(signal.take_profits) > 1:
            self._place_additional_tps(signal, lot, price, action_type)

        return {
            "success": True,
            "ticket": result.order,
            "symbol": signal.symbol,
            "direction": signal.direction.value,
            "lot": lot,
            "price": price,
        }

    def _place_additional_tps(self, signal: Signal, lot: float, price: float, action_type):
        """Place scaled TP orders for each extra take-profit level."""
        import MetaTrader5 as mt5
        partial_lot = round(lot / len(signal.take_profits), 2)

        for i, tp in enumerate(signal.take_profits[1:], start=2):
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": signal.symbol,
                "volume": max(partial_lot, mt5.symbol_info(signal.symbol).volume_min),
                "type": action_type,
                "price": price,
                "tp": tp,
                "sl": signal.stop_loss,
                "deviation": 20,
                "magic": 20240101,
                "comment": f"TeleTrader TP{i} #{signal.source_message_id}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            mt5.order_send(request)

    def _simulate(self, signal: Signal) -> Dict[str, Any]:
        """Simulation mode — logs trade without executing."""
        lot = signal.lot_size or self.settings.default_lot_size
        logger.info(
            f"[SIMULATION] Would execute: {signal.direction.value} "
            f"{lot} {signal.symbol} @ "
            f"{'MKT' if not signal.entry_price else signal.entry_price} | "
            f"SL: {signal.stop_loss} | TP: {signal.take_profits}"
        )
        return {
            "success": True,
            "simulated": True,
            "ticket": 0,
            "symbol": signal.symbol,
            "direction": signal.direction.value,
            "lot": lot,
            "price": signal.entry_price or 0,
        }

    def get_open_positions(self):
        if not MT5_AVAILABLE:
            return []
        positions = mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == 0 else "SELL",
                "volume": p.volume,
                "open_price": p.price_open,
                "current_price": p.price_current,
                "profit": p.profit,
                "sl": p.sl,
                "tp": p.tp,
            }
            for p in positions
        ]

    def get_account_info(self):
        if not MT5_AVAILABLE:
            return {"balance": 0, "equity": 0, "margin": 0, "free_margin": 0, "currency": "USD"}
        info = mt5.account_info()
        if info is None:
            return {}
        return {
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "free_margin": info.margin_free,
            "currency": info.currency,
            "profit": info.profit,
        }

    # ── Close / Cancel methods ────────────────────────────────────────────────

    def close_position(self, ticket: int) -> Dict[str, Any]:
        """
        Market-close a single open position by MT5 ticket number.
        Returns {"success": True, "ticket": ..., "price": ..., "profit": ...}
        or      {"success": False, "error": ...}
        """
        if not MT5_AVAILABLE:
            logger.info(f"[SIMULATION] Would close ticket={ticket}")
            return {"success": True, "simulated": True, "ticket": ticket, "price": 0, "profit": 0}

        position = self._get_position_by_ticket(ticket)
        if position is None:
            msg = f"Ticket {ticket} not found in open positions"
            logger.error(msg)
            return {"success": False, "error": msg}

        # Closing direction is opposite to opening direction
        close_type = mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(position.symbol)
        price = tick.bid if position.type == 0 else tick.ask  # BUY closes at bid, SELL at ask

        request = {
            "action":   mt5.TRADE_ACTION_DEAL,
            "symbol":   position.symbol,
            "volume":   position.volume,
            "type":     close_type,
            "position": ticket,
            "price":    price,
            "deviation": 20,
            "magic":    20240101,
            "comment":  f"TeleTrader close #{ticket}",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            msg = f"Close failed: retcode={result.retcode}, comment={result.comment}"
            logger.error(msg)
            return {"success": False, "error": msg, "retcode": result.retcode}

        logger.info(
            f"Position closed: ticket={ticket} | {position.symbol} "
            f"@ {price} | profit={position.profit:.2f}"
        )
        return {
            "success": True,
            "ticket":  ticket,
            "symbol":  position.symbol,
            "price":   price,
            "profit":  position.profit,
        }

    def close_all_positions(self) -> Dict[str, Any]:
        """
        Market-close every open position on the account.
        Returns a summary dict with per-ticket results.
        """
        if not MT5_AVAILABLE:
            logger.info("[SIMULATION] Would close all positions")
            return {"success": True, "simulated": True, "closed": [], "failed": []}

        positions = mt5.positions_get()
        if not positions:
            logger.info("close_all: no open positions found")
            return {"success": True, "closed": [], "failed": []}

        closed, failed = [], []
        for pos in positions:
            result = self.close_position(pos.ticket)
            if result["success"]:
                closed.append(pos.ticket)
            else:
                failed.append({"ticket": pos.ticket, "error": result.get("error")})

        logger.info(f"close_all complete: {len(closed)} closed, {len(failed)} failed")
        return {"success": len(failed) == 0, "closed": closed, "failed": failed}

    def cancel_pending_order(self, ticket: int) -> Dict[str, Any]:
        """
        Delete a pending (limit/stop) order by ticket.
        """
        if not MT5_AVAILABLE:
            logger.info(f"[SIMULATION] Would cancel pending order ticket={ticket}")
            return {"success": True, "simulated": True, "ticket": ticket}

        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order":  ticket,
            "comment": f"TeleTrader cancel #{ticket}",
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            msg = f"Cancel failed: retcode={result.retcode}, comment={result.comment}"
            logger.error(msg)
            return {"success": False, "error": msg, "retcode": result.retcode}

        logger.info(f"Pending order cancelled: ticket={ticket}")
        return {"success": True, "ticket": ticket}

    def close_positions_by_symbol_and_direction(self, symbol: str, direction: str) -> dict:
        """
        Market-close all open positions for a specific symbol AND direction.
        direction: "BUY" or "SELL"
        Returns {"success": True, "closed": [...], "failed": [...]}
        """
        if not MT5_AVAILABLE:
            logger.info(f"[SIMULATION] Would close all {direction} {symbol} positions")
            return {"success": True, "simulated": True, "closed": [], "failed": []}

        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            msg = f"No open positions found for {symbol}"
            logger.warning(msg)
            return {"success": False, "error": msg, "closed": [], "failed": []}

        # MT5 position type: 0 = BUY, 1 = SELL
        target_type = 0 if direction.upper() == "BUY" else 1
        matching = [p for p in positions if p.type == target_type]

        if not matching:
            msg = f"No open {direction} positions found for {symbol}"
            logger.warning(msg)
            return {"success": False, "error": msg, "closed": [], "failed": []}

        closed, failed = [], []
        for pos in matching:
            result = self.close_position(pos.ticket)
            if result["success"]:
                closed.append(pos.ticket)
            else:
                failed.append({"ticket": pos.ticket, "error": result.get("error")})

        logger.info(f"close_by_symbol_direction {direction} {symbol}: {len(closed)} closed, {len(failed)} failed")
        return {"success": len(failed) == 0, "closed": closed, "failed": failed}

    def close_positions_by_symbol(self, symbol: str) -> dict:
        """
        Market-close all open positions for a specific symbol.
        Returns {"success": True, "closed": [...], "failed": [...]}
        """
        if not MT5_AVAILABLE:
            logger.info(f"[SIMULATION] Would close all {symbol} positions")
            return {"success": True, "simulated": True, "closed": [], "failed": []}

        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            msg = f"No open positions found for {symbol}"
            logger.warning(msg)
            return {"success": False, "error": msg, "closed": [], "failed": []}

        closed, failed = [], []
        for pos in positions:
            result = self.close_position(pos.ticket)
            if result["success"]:
                closed.append(pos.ticket)
            else:
                failed.append({"ticket": pos.ticket, "error": result.get("error")})

        logger.info(f"close_by_symbol {symbol}: {len(closed)} closed, {len(failed)} failed")
        return {"success": len(failed) == 0, "closed": closed, "failed": failed}
    
    def _to_mt5_symbol(self, symbol: str) -> str:
        """Append broker suffix if not already present."""
        suffix = self.settings.mt5_symbol_suffix
        if not suffix:
            return symbol
        if symbol.upper().endswith(suffix.upper()):
            return symbol
        return symbol + suffix

    def _get_position_by_ticket(self, ticket: int):
        """Return MT5 position object for a ticket, or None if not found."""
        positions = mt5.positions_get(ticket=ticket)
        if positions and len(positions) > 0:
            return positions[0]
        return None
