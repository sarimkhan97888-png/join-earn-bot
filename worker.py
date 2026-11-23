"""
Profitix Ghost - dedicated 24/7 auto-reply engine.

Run modes
  * Standalone (Kaggle 30GB):  python worker.py        -> asyncio.run(main())
  * Embedded (ENABLE_WORKER=true on app.py): app.py calls run_engine()

Safety
  * LEASE: only ONE engine in the world can hold the lease row in Neon at a time, so even if
    Render and Kaggle are both started with the worker enabled, replies are never duplicated.
    The lease row doubles as a heartbeat (shown in /health and the admin panel).

Smart "reply only when I'm offline" mode
  The owner counts as ONLINE (auto-reply is skipped) when, inside the grace window or the active window:
    1. the owner sent a message from another device / the Telegram app,
    2. the owner read the incoming message (or replied in that chat),
    3. Telegram reports the owner's own status as "online" (best effort, can be disabled).
"""
import asyncio
import logging
import os
import random
import signal
import socket
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

import httpx
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import InputUserSelf, User, UserStatusOnline

import config
import database as db

log = logging.getLogger("worker")

DEAD_SESSION_ERRORS = (
    AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionRevokedError,
    UserDeactivatedBanError, UserDeactivatedError,
)
SETTINGS_TTL = 15.0
LEASE_NAME = "engine"
LEASE_TTL = 90
LEASE_RENEW_INTERVAL = 15
START_CONCURRENCY = 5
CONNECT_TIMEOUT = 30
STATUS_CACHE_TTL = 10.0
SELF_STATUS_TTL = 15.0
INSTANCE_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

Spec = Tuple[Callable, object]

_clients: Dict[int, TelegramClient] = {}
_handlers: Dict[int, List[Spec]] = {}
_self_ids: Dict[int, int] = {}
_stamps: Dict[int, object] = {}
_expiry: Dict[int, float] = {}
_settings_cache: Dict[int, Tuple[float, dict]] = {}
_last_reply: Dict[Tuple[int, int], float] = {}
_owner_active: Dict[int, float] = {}
_owner_replied: Dict[Tuple[int, int], float] = {}
_owner_read: Dict[Tuple[int, int], Tuple[int, float]] = {}
_bot_sending: Dict[Tuple[int, int], float] = {}
_last_self_send: Dict[int, float] = {}
_self_status: Dict[int, Tuple[float, bool]] = {}
_tasks: Set[asyncio.Task] = set()
_user_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_sem = asyncio.Semaphore(config.REPLY_CONCURRENCY)
_notifier: Optional[Callable[[int, str], Awaitable[bool]]] = None
_engine_active = False
_lease_held = False
_http: Optional[httpx.AsyncClient] = None
_status_cache: Tuple[float, bool, int] = (0.0, False, 0)


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


def is_engine_active() -> bool:
    return _engine_active


def is_running(user_id: int) -> bool:
    client = _clients.get(user_id)
    return bool(client and client.is_connected() and _expiry.get(user_id, 0) > time.time())


def invalidate_settings(user_id: int) -> None:
    _settings_cache.pop(user_id, None)


async def engine_status() -> Tuple[bool, int]:
    """(engine_alive, active_clients) - works from ANY process through the lease heartbeat."""
    global _status_cache
    if _engine_active:
        return True, len(_clients)
    now = time.monotonic()
    ts, alive, count = _status_cache
    if now - ts < STATUS_CACHE_TTL:
        return alive, count
    alive, count = False, 0
    try:
        async with db.pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT active_clients, (expires_at > NOW()) AS alive FROM worker_lease WHERE name = $1",
                LEASE_NAME,
            )
        if row and row["alive"]:
            alive, count = True, int(row["active_clients"])
    except Exception:
        log.debug("engine_status lookup failed")
    _status_cache = (now, alive, count)
    return alive, count


async def _safe_disconnect(client: TelegramClient) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


async def _connect(client: TelegramClient) -> None:
    await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)


# ------------------------------------------------------------------ lease (single engine guarantee)
async def _renew_lease() -> bool:
    global _lease_held
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO worker_lease (name, holder, active_clients, expires_at, updated_at)
            VALUES ($1, $2, $3, NOW() + $4::int * INTERVAL '1 second', NOW())
            ON CONFLICT (name) DO UPDATE
            SET holder = EXCLUDED.holder, active_clients = EXCLUDED.active_clients,
                expires_at = EXCLUDED.expires_at, updated_at = NOW()
            WHERE worker_lease.holder = EXCLUDED.holder OR worker_lease.expires_at < NOW()
            RETURNING holder
            """,
            LEASE_NAME, INSTANCE_ID, len(_clients), LEASE_TTL,
        )
    _lease_held = row is not None
    return _lease_held


async def _release_lease() -> None:
    global _lease_held
    if not _lease_held:
        return
    _lease_held = False
    try:
        async with db.pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE worker_lease SET expires_at = NOW() - INTERVAL '1 second', active_clients = 0, updated_at = NOW()
                WHERE name = $1 AND holder = $2
                """,
                LEASE_NAME, INSTANCE_ID,
            )
    except Exception:
        log.warning("Could not release engine lease")


async def _lease_loop() -> None:
    global _engine_active
    announced_standby = False
    while True:
        try:
            holds = await _renew_lease()
            if holds and not _engine_active:
                _engine_active = True
                announced_standby = False
                log.info("Engine lease ACQUIRED by %s", INSTANCE_ID)
            elif not holds:
                if _engine_active:
                    log.error("Engine lease LOST - stopping all clients to avoid duplicate replies")
                    _engine_active = False
                    await _stop_all()
                if not announced_standby:
                    log.warning("Another engine holds the lease - standing by (no replies from this process)")
                    announced_standby = True
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Lease renewal failed")
        await asyncio.sleep(LEASE_RENEW_INTERVAL)


# ------------------------------------------------------------------ owner presence (offline-only mode)
def _wrap(user_id: int, fn: Callable) -> Callable:
    async def handler(event) -> None:
        try:
            await fn(user_id, event)
        except Exception:
            log.exception("Handler error for user %s", user_id)

    return handler


async def _on_outgoing(user_id: int, event) -> None:
    """The owner typed/sent something from the Telegram app (our own auto-replies are excluded)."""
    now = time.time()
    chat_id = event.chat_id
    if now < _bot_sending.get((user_id, chat_id), 0):
        return
    _owner_active[user_id] = now
    _owner_replied[(user_id, chat_id)] = now


async def _on_read(user_id: int, event) -> None:
    """The owner opened a chat and read messages."""
    now = time.time()
    _owner_active[user_id] = now
    try:
        chat_id = event.chat_id
        if chat_id is not None:
            _owner_read[(user_id, chat_id)] = (int(event.max_id or 0), now)
    except Exception:
        pass


async def _self_online(user_id: int) -> bool:
    if not config.OFFLINE_STATUS_CHECK:
        return False
    now = time.time()
    if now - _last_self_send.get(user_id, 0) < 330:
        return False  # our own send can flash "online"; ignore the status shortly after it
    cached = _self_status.get(user_id)
    if cached and now - cached[0] < SELF_STATUS_TTL:
        return cached[1]
    online = False
    client = _clients.get(user_id)
    if client:
        try:
            result = await client(GetUsersRequest([InputUserSelf()]))
            online = bool(result) and isinstance(getattr(result[0], "status", None), UserStatusOnline)
        except Exception:
            online = False
    _self_status[user_id] = (now, online)
    return online


async def _owner_busy(user_id: int, chat_id: int, msg_id: int, since: float) -> bool:
    now = time.time()
    key = (user_id, chat_id)
    if _owner_replied.get(key, 0) >= since:
        return True
    read = _owner_read.get(key)
    if read and read[0] >= msg_id:
        return True
    if now - _owner_active.get(user_id, 0) < config.OWNER_ACTIVE_WINDOW_SECONDS:
        return True
    return await _self_online(user_id)


# ------------------------------------------------------------------ incoming messages
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


async def _on_message(user_id: int, event) -> None:
    if not event.is_private or event.out:  # ignore groups, channels and own messages
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

    only_offline = bool(cfg.get("only_when_offline", True))
    chat_id, msg_id = event.chat_id, int(event.id)
    if only_offline and await _owner_busy(user_id, chat_id, msg_id, now):
        return  # owner is online right now: stay silent (cooldown NOT consumed)

    reply = _pick_reply(cfg, event.raw_text or "")
    if not reply:
        return

    _last_reply[key] = now
    task = asyncio.create_task(_send_reply(user_id, chat_id, sender_id, msg_id, now, reply, cfg, only_offline))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _send_reply(
    user_id: int, chat_id: int, sender_id: int, msg_id: int, incoming_ts: float,
    reply: str, cfg: dict, only_offline: bool,
) -> None:
    key = (user_id, sender_id)
    client = _clients.get(user_id)
    if not client:
        _last_reply.pop(key, None)
        return
    lo = max(1, min(int(cfg.get("delay_min") or config.REPLY_DELAY_MIN), 30))
    hi = max(lo, min(int(cfg.get("delay_max") or config.REPLY_DELAY_MAX), 30))
    delay = random.uniform(lo, hi)
    try:
        if only_offline:
            await asyncio.sleep(config.OFFLINE_GRACE_SECONDS)  # give the owner a chance to answer first
            if await _owner_busy(user_id, chat_id, msg_id, incoming_ts):
                _last_reply.pop(key, None)
                return
        async with _sem:
            async with client.action(chat_id, "typing"):  # human typing simulation
                await asyncio.sleep(delay)
                if only_offline and await _owner_busy(user_id, chat_id, msg_id, incoming_ts):
                    _last_reply.pop(key, None)
                    return
                _bot_sending[(user_id, chat_id)] = time.time() + 30
                await client.send_message(chat_id, reply)
            _last_self_send[user_id] = time.time()
            _bot_sending[(user_id, chat_id)] = time.time() + 5
        await db.increment_replies(user_id)
    except FloodWaitError as exc:
        log.warning("FloodWait %ss for user %s", exc.seconds, user_id)
        _last_reply.pop(key, None)
    except DEAD_SESSION_ERRORS:
        log.warning("Session for user %s is no longer valid", user_id)
        await _drop_dead_session(user_id)
    except Exception:
        log.exception("Reply failed for user %s", user_id)
        _last_reply.pop(key, None)


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
    """Starts the auto-reply client. Does nothing unless THIS process holds the engine lease."""
    if not _engine_active:
        if client is not None:
            await _safe_disconnect(client)  # never keep a second live connection on the same session
        return False
    async with _user_locks[user_id]:
        await _stop_locked(user_id)
        row = await db.get_tg_session(user_id)
        epoch = await _expiry_epoch(user_id)
        settings = await db.get_settings(user_id)
        if not row or not epoch or not settings.get("is_active"):
            if client is not None:
                await _safe_disconnect(client)
            return False

        if client is None:
            client = new_client(row["session_string"])
            try:
                await _connect(client)
                authorized = await client.is_user_authorized()
            except DEAD_SESSION_ERRORS:
                authorized = False
            except Exception:
                log.warning("Could not connect client for user %s (will retry)", user_id)
                await _safe_disconnect(client)
                return False
            if not authorized:
                await _safe_disconnect(client)
                await db.delete_tg_session(user_id)
                return False

        try:
            me = await client.get_me()
        except DEAD_SESSION_ERRORS:
            await _safe_disconnect(client)
            await db.delete_tg_session(user_id)
            return False
        except Exception:
            log.warning("get_me failed for user %s (will retry)", user_id)
            await _safe_disconnect(client)
            return False
        if me is None:
            await _safe_disconnect(client)
            await db.delete_tg_session(user_id)
            return False

        specs: List[Spec] = [
            (_wrap(user_id, _on_message), events.NewMessage(incoming=True)),
            (_wrap(user_id, _on_outgoing), events.NewMessage(outgoing=True)),
            (_wrap(user_id, _on_read), events.MessageRead(inbox=True)),
        ]
        for fn, builder in specs:
            client.add_event_handler(fn, builder)
        _clients[user_id] = client
        _handlers[user_id] = specs
        _self_ids[user_id] = me.id
        _stamps[user_id] = row["connected_at"]
        _expiry[user_id] = epoch
        invalidate_settings(user_id)
        log.info("Auto-reply ONLINE for user %s", user_id)
        return True


async def _remote_logout(user_id: int) -> None:
    """Logs the Telegram session out using a temporary client (used when no live client is held here)."""
    row = await db.get_tg_session(user_id)
    if not row or not config.API_ID:
        return
    client = new_client(row["session_string"])
    try:
        await _connect(client)
        if await client.is_user_authorized():
            await client.log_out()
    except Exception:
        log.warning("Remote logout failed for user %s", user_id)
    finally:
        await _safe_disconnect(client)


async def _stop_locked(user_id: int, log_out: bool = False) -> None:
    client = _clients.pop(user_id, None)
    specs = _handlers.pop(user_id, [])
    for table in (_self_ids, _stamps, _expiry, _settings_cache, _owner_active, _self_status, _last_self_send):
        table.pop(user_id, None)
    for table in (_last_reply, _owner_replied, _owner_read, _bot_sending):
        for key in [k for k in table if k[0] == user_id]:
            table.pop(key, None)
    if client is None:
        if log_out:
            await _remote_logout(user_id)
        return
    for fn, builder in specs:
        try:
            client.remove_event_handler(fn, builder)
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


async def _stop_all() -> None:
    await asyncio.gather(*(stop_user_client(u) for u in list(_clients)), return_exceptions=True)


async def ensure_running(user_id: int) -> bool:
    """After a payment: resumes instantly when this process is the engine, otherwise the engine's next sync does it."""
    if not _engine_active:
        return False
    client = _clients.get(user_id)
    if client and client.is_connected():
        await refresh_expiry(user_id)
        return True
    return await start_user_client(user_id)


# ------------------------------------------------------------------ Neon sync (picks up paid / connected / paused users)
async def _fetch_wanted() -> Dict[int, dict]:
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.user_id, s.connected_at, u.plan_expires_at
            FROM tg_sessions s
            JOIN users u ON u.telegram_user_id = s.user_id
            LEFT JOIN auto_reply_settings a ON a.user_id = s.user_id
            WHERE u.plan_status = 'active' AND u.plan_expires_at > NOW()
              AND COALESCE(a.is_active, TRUE)
            """
        )
    return {r["user_id"]: dict(r) for r in rows}


async def sync_once() -> None:
    wanted = await _fetch_wanted()

    # 1) stop paused / expired / disconnected / re-linked accounts, refresh expiry of renewed ones
    for uid in list(_clients):
        row = wanted.get(uid)
        if row is None or row["connected_at"] != _stamps.get(uid):
            await stop_user_client(uid)
            log.info("Sync: unloaded user %s (paused, expired, disconnected or re-linked)", uid)
        else:
            _expiry[uid] = row["plan_expires_at"].timestamp()

    # 2) start newly connected / newly paid accounts
    todo = [uid for uid in wanted if uid not in _clients]
    gate = asyncio.Semaphore(START_CONCURRENCY)

    async def start_one(uid: int) -> None:
        async with gate:
            try:
                await start_user_client(uid)
            except Exception:
                log.exception("Start failed for user %s", uid)

    if todo:
        await asyncio.gather(*(start_one(u) for u in todo))

    # 3) reconnect dropped sockets (network blips)
    down = [(uid, c) for uid, c in _clients.items() if not c.is_connected()]

    async def heal(uid: int, client: TelegramClient) -> None:
        async with gate:
            try:
                await _connect(client)
            except DEAD_SESSION_ERRORS:
                await _drop_dead_session(uid)
            except Exception:
                log.warning("Reconnect failed for user %s (will retry)", uid)

    if down:
        await asyncio.gather(*(heal(u, c) for u, c in down))


async def sync_loop() -> None:
    last = 0.0
    while True:
        if _engine_active and time.monotonic() - last >= config.DB_SYNC_INTERVAL_SECONDS:
            last = time.monotonic()
            try:
                await sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Sync cycle failed (will retry)")
        await asyncio.sleep(1)


# ------------------------------------------------------------------ expiry cleanup + notifications (every 10 min)
async def _notify_expired_http(user_id: int, plan: str) -> bool:
    """Bot message without aiogram, so the Kaggle worker stays dependency-light."""
    global _http
    if not config.BOT_TOKEN:
        return False
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    label = "trial" if plan == "trial" else "subscription"
    payload: dict = {
        "chat_id": user_id,
        "parse_mode": "HTML",
        "text": (
            f"<b>Your Ghost has been paused.</b>\n\nYour {label} has expired and auto-replies are switched off. "
            "Resume instantly in one tap, your settings are saved."
        ),
    }
    if config.WEBAPP_URL:
        payload["reply_markup"] = {
            "inline_keyboard": [[{
                "text": "Pay ₹49/mo to Resume Instantly",
                "web_app": {"url": f"{config.WEBAPP_URL}/?tab=plans"},
            }]]
        }
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    for _ in range(2):
        try:
            resp = await _http.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("Notify failed: %s", exc.__class__.__name__)
            return False
        if resp.status_code == 200:
            return True
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 3))
            except Exception:
                wait = 3
            await asyncio.sleep(min(wait, 30))
            continue
        return False  # 403 blocked / 400 chat not found: nothing more to try
    return False


async def cleanup_once() -> None:
    if not _engine_active:
        return
    now = time.time()

    for row in await db.list_due_for_expiry():
        uid = row["telegram_user_id"]
        await stop_user_client(uid)
        await db.mark_expired(uid)
        await db.log_event(uid, "plan_expired", {"plan": row["plan"]})
        log.info("Expired and unloaded user %s", uid)
    for uid in [u for u, ts in _expiry.items() if ts <= now]:
        await stop_user_client(uid)

    if _notifier:
        for row in await db.list_expired_unnotified():
            uid = row["telegram_user_id"]
            try:
                delivered = await _notifier(uid, row["plan"])
                if delivered:
                    await db.log_event(uid, "expiry_notified", {"plan": row["plan"]})
            except Exception:
                log.exception("Expiry notification failed for %s", uid)
            await db.mark_expiry_notified(uid)  # mark either way so we never spam
            await asyncio.sleep(0.05)

    for table in (_last_reply,):
        for key in [k for k, ts in table.items() if now - ts > 3700]:
            table.pop(key, None)
    for table in (_owner_replied, _bot_sending):
        for key in [k for k, ts in table.items() if now - ts > 7200]:
            table.pop(key, None)
    for key in [k for k, (_, ts) in _owner_read.items() if now - ts > 7200]:
        _owner_read.pop(key, None)


async def cleanup_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Cleanup cycle failed")
        await asyncio.sleep(config.CLEANUP_INTERVAL_SECONDS)


# ------------------------------------------------------------------ engine + entrypoints
async def run_engine(notifier: Optional[Callable[[int, str], Awaitable[bool]]] = None) -> None:
    """Lease keeper + Neon sync + expiry cleanup. Used by app.py (embedded) and main() (standalone)."""
    if notifier:
        set_notifier(notifier)
    tasks = [
        asyncio.create_task(_lease_loop(), name="lease"),
        asyncio.create_task(sync_loop(), name="sync"),
        asyncio.create_task(cleanup_loop(), name="cleanup"),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await shutdown()


async def shutdown() -> None:
    global _engine_active, _http
    _engine_active = False
    await _stop_all()
    await _release_lease()
    if _http is not None:
        try:
            await _http.aclose()
        except Exception:
            pass
        _http = None


async def main() -> None:
    missing = [
        name for name, val in (
            ("DATABASE_URL", config.DATABASE_URL), ("API_ID", config.API_ID), ("API_HASH", config.API_HASH),
        ) if not val
    ]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    if not config.BOT_TOKEN:
        log.warning("BOT_TOKEN missing: expiry notifications are disabled")

    for attempt in range(1, 7):
        try:
            await db.init_db()
            break
        except Exception:
            if attempt == 6:
                raise
            log.warning("Neon is waking up (attempt %s/6), retrying...", attempt)
            await asyncio.sleep(3 * attempt)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    log.info("Standalone worker %s starting (Neon sync every %ss)", INSTANCE_ID, config.DB_SYNC_INTERVAL_SECONDS)
    engine = asyncio.create_task(run_engine(_notify_expired_http))
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({engine, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if engine in done:
            engine.result()  # crash => non-zero exit => kaggle_runner restarts us
    finally:
        waiter.cancel()
        if not engine.done():
            engine.cancel()
        await asyncio.gather(engine, waiter, return_exceptions=True)
        await shutdown()
        await db.close_db()


run_standalone = main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
