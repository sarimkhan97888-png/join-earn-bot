"""
Profitix Ghost - aiogram 3.x Telegram bot (long polling).
Opens the Mini App and sends the "Pay Rs.49/mo to Resume Instantly" message on expiry.
"""
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

import config
import database as db
import worker

log = logging.getLogger("bot")

router = Router()
dp = Dispatcher()
dp.include_router(router)

_bot: Optional[Bot] = None


def get_bot() -> Optional[Bot]:
    global _bot
    if _bot is None and config.BOT_TOKEN:
        _bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return _bot


def webapp_url(tab: Optional[str] = None) -> str:
    if not config.WEBAPP_URL:
        return ""
    return f"{config.WEBAPP_URL}/?tab={tab}" if tab else f"{config.WEBAPP_URL}/"


def open_app_keyboard(text: str = "Open Profitix Ghost", tab: Optional[str] = None) -> Optional[InlineKeyboardMarkup]:
    url = webapp_url(tab)
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]]
    )


# ------------------------------------------------------------------ handlers
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    await db.upsert_user(user.id, user.first_name or "", user.username or "")
    await message.answer(
        f"<b>Welcome to Profitix Ghost, {user.first_name or 'there'}!</b>\n\n"
        "Your 24/7 Telegram auto-reply assistant. It answers your chats while you are busy, "
        "asleep or offline, with human-like typing and smart cooldowns.\n\n"
        "Tap the button below to connect your account and start.",
        reply_markup=open_app_keyboard(),
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    status = await db.get_status(user.id)
    if status["status"] == "active":
        hours = status["seconds_left"] // 3600
        text = (
            f"<b>Plan:</b> {status['plan'].title()} (active)\n"
            f"<b>Time left:</b> {hours // 24}d {hours % 24}h\n"
            f"<b>Replies sent:</b> {status['replies_sent']}"
        )
    else:
        text = "Your Ghost is <b>paused</b>. Open the app to activate a plan."
    await message.answer(text, reply_markup=open_app_keyboard("Open App", "plans" if status["status"] != "active" else None))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Commands</b>\n/start - open the app\n/status - plan and reply stats\n/help - this message\n\n"
        f"Support: @{config.SUPPORT_USERNAME}",
        reply_markup=open_app_keyboard(),
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not message.from_user or message.from_user.id != config.ADMIN_ID:
        return
    await message.answer(f"Active workers in RAM: <b>{worker.active_count()}</b>")


# ------------------------------------------------------------------ outgoing notifications
async def notify_expired(user_id: int, plan: str) -> bool:
    """Sent by the cleanup scheduler once a plan or trial has expired."""
    bot = get_bot()
    if not bot:
        return False
    kb = open_app_keyboard("Pay ₹49/mo to Resume Instantly", "plans")
    label = "trial" if plan == "trial" else "subscription"
    try:
        await bot.send_message(
            user_id,
            f"<b>Your Ghost has been paused.</b>\n\nYour {label} has expired and auto-replies are switched off. "
            "Resume instantly in one tap, your settings are saved.",
            reply_markup=kb,
        )
        return True
    except TelegramForbiddenError:
        return False  # user blocked the bot
    except TelegramAPIError:
        log.warning("Could not notify user %s", user_id)
        return False


# ------------------------------------------------------------------ lifecycle
async def run_polling() -> None:
    bot = get_bot()
    if not bot:
        log.warning("BOT_TOKEN missing, bot polling disabled")
        return
    await bot.delete_webhook(drop_pending_updates=True)
    if config.WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Open Ghost", web_app=WebAppInfo(url=webapp_url()))
            )
        except TelegramAPIError:
            log.warning("Could not set menu button")
    log.info("Bot polling started")
    await dp.start_polling(bot, handle_signals=False, allowed_updates=["message"])


async def close_bot() -> None:
    global _bot
    if _bot is not None:
        try:
            await _bot.session.close()
        finally:
            _bot = None
