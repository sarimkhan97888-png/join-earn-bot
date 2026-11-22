"""
Profitix Ghost - 24/7 Telethon auto-reply worker + RAM cleanup scheduler.

* One live TelegramClient per active subscriber (held in RAM only while the plan is valid).
* Outgoing replies are throttled by asyncio.Semaphore(10) so bursts never spike CPU/RAM.
* Only the counter users.total_replies_sent is written; no message text is stored.
* Every 10 minutes expired clients are disconnected + unloaded and the user is notified.
"""
import asyncio
import logging
import random
import time
from collections import defaultdict
from datetime import datetime
from typing import Awaitable, Callable, Dict, Optional, Set, Tuple

from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession
from telethon.tl.types import User

import config
import database as db

log = logging.getLogger("worker")

DEAD_SESSION_ERRORS = (
    AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedError, UserDeactivatedBanError,
)
SETTINGS_TTL = 20.0

_clients: Dict[int, TelegramClient] = {}
_handlers: Dict[int, Callable] = {}
_self_ids: Dict[int, int] = {}
_expiry: Dict[int, float] = {}
_settings_cache: Dict[int, Tuple[float, dict]] = {}
_last_reply: Dict[Tuple[int, int], float] = {}
_tasks: Set[asyncio.Task] = set()
_user_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_sem = asyncio.Semaphore(config.REPLY_CONCURRENCY)
_notifier: Optional[Callable[[int, str], Awaitable[bool]]] = None


# ------------------------------------------------------------------ public helpers
def new_client(session_string: str = "") -> TelegramClient:
    return TelegramClient(
        StringSession(session_string), config.API_ID, config.API_HASH,
        device_model="Profitix Ghost", app_version="1.0", system_version="Linux",
        lang_code="en", system_lang_code="en",
    )


def set_notifier(fn: Callable[[int, str], Awaitable[bool]]) -> None:
    global _notifier
    _notifier = fn


def active_count() -> int:
    return len(_clients)


def is_running(user_id: int) -> bool:
    client = _clients.get(user_id)
    return bool(client and client.is_connected() and _expiry.get(user_id, 0) > time.time())


def invalidate_settings(user_id: int) -> None:
    _settings_cache.pop(user_id, None)


async def _safe_disconnect(client: TelegramClient) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


# ------------------------------------------------------------------ message handling
async def _get_settings(user_id: int) -> dict:
    cached = _settings_cache.get(user_id)
    now = time.time()
    if cached and now - cached[0] < SETTINGS_TTL:
        return cached[1]
    data = await db.get_settings(user_id)
    _settings_cache[user_id] = (now, data)
    return data


def _pick_reply(cfg: dict, text: str) -> str:
    lowered = (text or "").lower()
    for rule in cfg.get("keyword_rules") or []:
        keyword = (rule.get("keyword") or "").strip().lower()
        reply = (rule.get("reply") or "").strip()
        if keyword and reply and keyword in lowered:
            return reply
    return (cfg.get("default_reply") or "").strip()


def _make_handler(user_id: int) -> Callable:
    async def handler(event) -> None:
        try:
            await _on_message(user_id, event)
        except Exception:
            log.exception("Handler error for user %s", user_id)

    return handler


async def _on_message(user_id: int, event) -> None:
    # Ignore groups, channels and our own outgoing messages
    if not event.is_private or event.out:
        return
    if _expiry.get(user_id, 0) <= time.time():
        return
    sender_id = event.sender_id
    if not sender_id or sender_id == _self_ids.get(user_id) or sender_id == 777000:
        return
    sender = await event.get_sender()
    if not isinstance(sender, User) or sender.bot or sender.is_self or sender.deleted:
        return

    cfg = await _get_settings(user_id)
    if not cfg.get("is_active"):
        return

    now = time.time()
    key = (user_id, sender_id)
    cooldown = int(cfg.get("cooldown_seconds") or config.DEFAULT_COOLDOWN_SECONDS)
    if now - _last_reply.get(key, 0.0) < cooldown:
        return

    reply = _pick_reply(cfg, event.raw_text or "")
    if not reply:
        return

    _last_reply[key] = now
    task = asyncio.create_task(_send_reply(user_id, event.chat_id, sender_id, reply, cfg))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _send_reply(user_id: int, chat_id: int, sender_id: int, reply: str, cfg: dict) -> None:
    client = _clients.get(user_id)
    if not client:
        return
    lo = max(1, min(int(cfg.get("delay_min") or config.REPLY_DELAY_MIN), 30))
    hi = max(lo, min(int(cfg.get("delay_max") or config.REPLY_DELAY_MAX), 30))
    delay = random.uniform(lo, hi)
    async with _sem:
        try:
            async with client.action(chat_id, "typing"):  # human typing simulation
                await asyncio.sleep(delay)
                await client.send_message(chat_id, reply)
            await db.increment_replies(user_id)
        except FloodWaitError as exc:
            log.warning("FloodWait %ss for user %s", exc.seconds, user_id)
            _last_reply.pop((user_id, sender_id), None)
        except DEAD_SESSION_ERRORS:
            log.warning("Session for user %s is no longer valid", user_id)
            await _drop_dead_session(user_id)
        except Exception:
            log.exception("Reply failed for user %s", user_id)
            _last_reply.pop((user_id, sender_id), None)


# ------------------------------------------------------------------ client lifecycle
async def _drop_dead_session(user_id: int) -> None:
    await stop_user_client(user_id)
    await db.delete_tg_session(user_id)


async def _expiry_epoch(user_id: int) -> Optional[float]:
    status = await db.get_status(user_id)
    if status["status"] != "active" or not status["expires_at"]:
        return None
    return datetime.fromisoformat(status["expires_at"]).timestamp()


async def refresh_expiry(user_id: int) -> None:
    epoch = await _expiry_epoch(user_id)
    if epoch and user_id in _clients:
        _expiry[user_id] = epoch


async def start_user_client(user_id: int, client: Optional[TelegramClient] = None) -> bool:
    """Starts (or restarts) the auto-reply client. A live client can be passed in after OTP login."""
    async with _user_locks[user_id]:
        await _stop_locked(user_id)
        owned_here = client is None
        if client is None:
            row = await db.get_tg_session(user_id)
            if not row:
                return False
            client = new_client(row["session_string"])
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await _safe_disconnect(client)
                    await db.delete_tg_session(user_id)
                    return False
            except DEAD_SESSION_ERRORS:
                await _safe_disconnect(client)
                await db.delete_tg_session(user_id)
                return False
            except Exception:
                log.exception("Could not start client for user %s", user_id)
                await _safe_disconnect(client)
                return False

        epoch = await _expiry_epoch(user_id)
        if not epoch:
            await _safe_disconnect(client)
            return False
        try:
            me = await client.get_me()
        except DEAD_SESSION_ERRORS:
            await _safe_disconnect(client)
            await db.delete_tg_session(user_id)
            return False
        except Exception:
            log.exception("get_me failed for user %s", user_id)
            await _safe_disconnect(client)
            return False

        handler = _make_handler(user_id)
        client.add_event_handler(handler, events.NewMessage(incoming=True))
        _clients[user_id] = client
        _handlers[user_id] = handler
        _self_ids[user_id] = me.id
        _expiry[user_id] = epoch
        invalidate_settings(user_id)
        log.info("Worker started for user %s (%s)", user_id, "restored" if owned_here else "fresh login")
        return True


async def _stop_locked(user_id: int, log_out: bool = False) -> None:
    client = _clients.pop(user_id, None)
    handler = _handlers.pop(user_id, None)
    _self_ids.pop(user_id, None)
    _expiry.pop(user_id, None)
    _settings_cache.pop(user_id, None)
    for key in [k for k in _last_reply if k[0] == user_id]:
        _last_reply.pop(key, None)
    if client:
        if handler:
            try:
                client.remove_event_handler(handler)
            except Exception:
                pass
        if log_out:
            try:
                await client.log_out()
                return
            except Exception:
                pass
        await _safe_disconnect(client)


async def stop_user_client(user_id: int, log_out: bool = False) -> None:
    async with _user_locks[user_id]:
        await _stop_locked(user_id, log_out)


async def ensure_running(user_id: int) -> bool:
    """Called after a successful payment so the worker resumes instantly."""
    if user_id in _clients and _clients[user_id].is_connected():
        await refresh_expiry(user_id)
        return True
    return await start_user_client(user_id)


async def restore_all() -> None:
    """Boot-time restore of every subscriber whose plan is still valid."""
    sessions = await db.list_active_sessions()
    ok = 0
    for row in sessions:
        try:
            if await start_user_client(row["user_id"]):
                ok += 1
        except Exception:
            log.exception("Restore failed for user %s", row["user_id"])
        await asyncio.sleep(0.5)  # gentle stagger to avoid connect bursts
    log.info("Worker restore complete: %s/%s clients online", ok, len(sessions))


# ------------------------------------------------------------------ RAM auto-cleanup (every 10 min)
async def cleanup_once() -> None:
    now = time.time()

    # 1) Expire + unload clients whose time is up
    for row in await db.list_due_for_expiry():
        uid = row["telegram_user_id"]
        await stop_user_client(uid)
        await db.mark_expired(uid)
        log.info("Expired and unloaded user %s", uid)
    for uid in [u for u, ts in _expiry.items() if ts <= now]:
        await stop_user_client(uid)

    # 2) Tell expired users how to resume (WebApp button from the bot)
    if _notifier:
        for row in await db.list_expired_unnotified():
            uid = row["telegram_user_id"]
            try:
                await _notifier(uid, row["plan"])
            except Exception:
                log.exception("Expiry notification failed for %s", uid)
            await db.mark_expiry_notified(uid)  # mark either way so we never spam
            await asyncio.sleep(0.05)

    # 3) Prune stale cooldown entries (max cooldown is 1h)
    for key in [k for k, ts in _last_reply.items() if now - ts > 3700]:
        _last_reply.pop(key, None)

    # 4) Reconnect dropped clients, drop dead sessions
    for uid, client in list(_clients.items()):
        if client.is_connected():
            continue
        try:
            await client.connect()
        except DEAD_SESSION_ERRORS:
            await _drop_dead_session(uid)
        except Exception:
            log.warning("Reconnect failed for user %s", uid)


async def cleanup_loop() -> None:
    await asyncio.sleep(30)  # let the restore pass run first
    while True:
        try:
            await cleanup_once()
        except Exception:
            log.exception("Cleanup cycle failed")
        await asyncio.sleep(config.CLEANUP_INTERVAL_SECONDS)


async def shutdown() -> None:
    for uid in list(_clients):
        try:
            await stop_user_client(uid)
        except Exception:
            pass
