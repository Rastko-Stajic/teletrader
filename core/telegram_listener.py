"""
TelegramListener - connects as a Telegram user and listens to a private group.
Uses Telethon (user-client, not bot) so it works in groups where you're a member.

Handles two message types:
  - Open signal    → parsed by SignalParser, passed to on_signal()
  - Close/cancel   → parsed by CloseSignalParser, passed to on_close()
"""

import asyncio
from typing import Callable, Optional
from telethon import TelegramClient, events
from telethon.tl.types import Message
from core.signal_parser import SignalParser, CloseSignalParser
from core.signal import Signal
from core.logger import get_logger
from config.settings import Settings

logger = get_logger("telegram")


class TelegramListener:
    def __init__(
        self,
        settings: Settings,
        on_signal:       Callable,   # async (Signal) -> None
        on_close:        Callable,   # async (CloseSignal) -> None
        on_unrecognized: Callable,   # (text, message_id) -> None
        parser: SignalParser,
    ):
        self.settings       = settings
        self.on_signal      = on_signal
        self.on_close       = on_close
        self.on_unrecognized = on_unrecognized
        self.parser         = parser
        self.close_parser   = CloseSignalParser()

        self.client = TelegramClient(
            "teletrader_session",
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )

    async def start(self):
        await self.client.start(phone=self.settings.telegram_phone)
        logger.info("Telegram client authenticated.")

        group = await self.client.get_entity(self.settings.telegram_group_id)
        logger.info(f"Monitoring group: {getattr(group, 'title', group.id)}")

        @self.client.on(events.NewMessage(chats=self.settings.telegram_group_id))
        async def handler(event: events.NewMessage.Event):
            message: Message = event.message
            text = message.text or ""

            if not text.strip():
                return

            # Extract reply reference (key for close signal linking)
            reply_to_id: Optional[int] = None
            if message.reply_to and hasattr(message.reply_to, "reply_to_msg_id"):
                reply_to_id = message.reply_to.reply_to_msg_id

            logger.debug(
                f"Message [{message.id}]"
                f"{f' reply_to=[{reply_to_id}]' if reply_to_id else ''}: "
                f"{text[:80]}..."
            )

            loop = asyncio.get_event_loop()

            # ── Route: close/cancel check first (cheap regex) ─────────────────
            if self.close_parser.is_close_message(text):
                close_signal = await loop.run_in_executor(
                    None,
                    self.close_parser.parse,
                    text, message.id, reply_to_id,
                )
                if close_signal:
                    if asyncio.iscoroutinefunction(self.on_close):
                        await self.on_close(close_signal)
                    else:
                        await loop.run_in_executor(None, self.on_close, close_signal)
                    return  # don't also try to parse as open signal

            # ── Route: open signal ────────────────────────────────────────────
            signal = await loop.run_in_executor(
                None, self.parser.parse, text, message.id
            )
            if signal:
                if asyncio.iscoroutinefunction(self.on_signal):
                    await self.on_signal(signal)
                else:
                    await loop.run_in_executor(None, self.on_signal, signal)
            else:
                await loop.run_in_executor(None, self.on_unrecognized, text, message.id)

        logger.info("Listening for new messages...")
        await self.client.run_until_disconnected()

    async def stop(self):
        await self.client.disconnect()
        logger.info("Telegram client disconnected.")
