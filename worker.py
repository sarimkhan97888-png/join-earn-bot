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
import shutil
import socket
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
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
import webhooks

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
_tier: Dict[int, str] = {}
_diag_last: Dict[Tuple[int, str], float] = {}
_integ_cache: Dict[int, Tuple[float, Optional[dict]]] = {}
BRIDGE_DIR = Path(__file__).resolve().parent / "wa_bridge"


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
    """Telegram's own view of the owner: UserStatusOnline (app open / active) vs offline."""
    if not config.OFFLINE_STATUS_CHECK:
        return False
    now = time.time()
    if now - _last_self_send.get(user_id, 0) < 330:
        return False  # our own send can flash "online"; ignore the status shortly after it
    cached = _self_status.get(user_id)
    if cached and now - cached[0] < 8:
        return cached[1]
    online = False
    client = _clients.get(user_id)
    if client:
        try:
            result = await client(GetUsersRequest([InputUserSelf()]))
            status = getattr(result[0], "status", None) if result else None
            if isinstance(status, UserStatusOnline):
                expires = getattr(status, "expires", None)
                online = expires is None or expires.timestamp() > now
        except Exception:
            online = False
    _self_status[user_id] = (now, online)
    return online


async def _owner_block(user_id: int, chat_id: int, msg_id: int, since: float) -> Optional[str]:
    """None => the owner is OFFLINE (safe to reply). Otherwise the reason Ghost must stay silent."""
    now = time.time()
    key = (user_id, chat_id)
    if _owner_replied.get(key, 0) >= since:
        return "owner_replied"
    read = _owner_read.get(key)
    if read and read[0] >= msg_id:
        return "owner_read"
    if now - _owner_active.get(user_id, 0) < config.OWNER_ACTIVE_WINDOW_SECONDS:
        return "owner_active"
    if await _self_online(user_id):
        return "owner_status_online"
    return None


def _decide(user_id: int, outcome: str, reason: str, throttle: float = 30.0) -> None:
    """Records why Ghost replied / stayed silent (throttled per reason) for the Ghost-tab activity card."""
    now = time.time()
    if outcome != "sent":
        if now - _diag_last.get((user_id, reason), 0) < throttle:
            return
        _diag_last[(user_id, reason)] = now
    task = asyncio.create_task(db.log_decision(user_id, outcome, reason))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


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


async def _get_integration(user_id: int) -> Optional[dict]:
    """Webhook settings (Pro only), cached briefly. None when disabled."""
    if _tier.get(user_id) != "pro":
        return None
    cached = _integ_cache.get(user_id)
    now = time.time()
    if cached and now - cached[0] < SETTINGS_TTL:
        return cached[1]
    data = await db.get_integration(user_id)
    hook = data if data.get("webhook_enabled") and data.get("webhook_url") else None
    _integ_cache[user_id] = (now, hook)
    return hook


async def _deliver_webhook(user_id: int, hook: dict, payload: dict) -> Optional[dict]:
    try:
        result = await webhooks.post_event(hook["webhook_url"], hook.get("webhook_secret") or "", payload)
        await db.webhook_result(user_id, result["ok"], result["status"], result["error"])
        return result["json"] if result["ok"] else None
    except Exception:
        log.exception("Webhook delivery failed for user %s", user_id)
        return None


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

    text = event.raw_text or ""
    chat_id, msg_id = event.chat_id, int(event.id)

    # Pro webhook: always delivered (even when auto-reply is paused or the owner is online)
    hook = await _get_integration(user_id)
    hook_task: Optional[asyncio.Task] = None
    if hook:
        payload = webhooks.build_event(
            "telegram", user_id, sender_id, chat_id, msg_id, text,
            getattr(sender, "first_name", None), getattr(sender, "username", None),
        )
        hook_task = asyncio.create_task(_deliver_webhook(user_id, hook, payload))
        _tasks.add(hook_task)
        hook_task.add_done_callback(_tasks.discard)
    hook_decides = bool(hook and hook.get("webhook_reply"))

    cfg = await _get_settings(user_id)
    if not cfg.get("is_active"):
        _decide(user_id, "skipped", "paused")
        return

    now = time.time()
    key = (user_id, sender_id)
    cooldown = int(cfg.get("cooldown_seconds") or config.DEFAULT_COOLDOWN_SECONDS)
    if now - _last_reply.get(key, 0.0) < cooldown:
        _decide(user_id, "skipped", "cooldown")
        return

    only_offline = bool(cfg.get("only_when_offline", True))
    if only_offline:
        why = await _owner_block(user_id, chat_id, msg_id, now)
        if why:  # owner is online right now: stay silent (cooldown NOT consumed)
            _decide(user_id, "skipped", why)
            return

    reply = _pick_reply(cfg, text)
    if not reply and not hook_decides:
        _decide(user_id, "skipped", "no_reply_text")
        return

    peer = await event.get_input_chat()  # exact peer + access hash from this very update (no entity lookup needed)
    _last_reply[key] = now
    task = asyncio.create_task(
        _send_reply(user_id, peer, chat_id, sender_id, msg_id, now, reply, cfg, only_offline,
                    hook_task if hook_decides else None)
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _send_reply(
    user_id: int, peer, chat_id: int, sender_id: int, msg_id: int, incoming_ts: float,
    reply: str, cfg: dict, only_offline: bool, hook_task: Optional[asyncio.Task] = None,
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
            why = await _owner_block(user_id, chat_id, msg_id, incoming_ts)
            if why:
                _decide(user_id, "skipped", why)
                _last_reply.pop(key, None)
                return
        if hook_task is not None:  # the user's webhook decides the reply
            try:
                answer = await asyncio.wait_for(asyncio.shield(hook_task), 8)
            except Exception:
                answer = None
            if isinstance(answer, dict):
                if answer.get("skip"):
                    _decide(user_id, "skipped", "webhook_skip")
                    _last_reply.pop(key, None)
                    return
                override = answer.get("reply")
                if isinstance(override, str) and override.strip():
                    reply = override.strip()[:1000]
        if not reply:
            _last_reply.pop(key, None)
            return
        async with _sem:
            async with client.action(peer, "typing"):  # human typing simulation
                await asyncio.sleep(delay)
                if only_offline:
                    why = await _owner_block(user_id, chat_id, msg_id, incoming_ts)
                    if why:
                        _decide(user_id, "skipped", why)
                        _last_reply.pop(key, None)
                        return
                _bot_sending[(user_id, chat_id)] = time.time() + 30
                await client.send_message(peer, reply)
            _last_self_send[user_id] = time.time()
            _bot_sending[(user_id, chat_id)] = time.time() + 5
        _decide(user_id, "sent", "sent")
        await db.increment_replies(user_id)
    except FloodWaitError as exc:
        log.warning("FloodWait %ss for user %s", exc.seconds, user_id)
        _decide(user_id, "error", f"FloodWait {exc.seconds}s")
        _last_reply.pop(key, None)
    except DEAD_SESSION_ERRORS:
        log.warning("Session for user %s is no longer valid", user_id)
        _decide(user_id, "error", "session_invalid")
        await _drop_dead_session(user_id)
    except Exception as exc:
        log.exception("Reply failed for user %s", user_id)
        _decide(user_id, "error", exc.__class__.__name__)
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
        info = await db.get_status(user_id)
        epoch = datetime.fromisoformat(info["expires_at"]).timestamp() if info["status"] == "active" and info["expires_at"] else None
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
        _tier[user_id] = info["plan"]
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
    for table in (_self_ids, _stamps, _expiry, _settings_cache, _owner_active, _self_status, _last_self_send, _tier, _integ_cache):
        table.pop(user_id, None)
    for table in (_last_reply, _owner_replied, _owner_read, _bot_sending, _diag_last):
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
            SELECT s.user_id, s.connected_at, u.plan, u.plan_expires_at
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
            _tier[uid] = row["plan"]

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


# ------------------------------------------------------------------ commands from the web gateway
async def _run_command(cmd: dict) -> None:
    uid = int(cmd["user_id"])
    if cmd["cmd"] != "test_reply":
        return
    client = _clients.get(uid)
    if not client:
        await db.log_decision(uid, "test_failed", "not_running")
        return
    try:
        settings = await db.get_settings(uid)
        me_id = _self_ids.get(uid)
        _bot_sending[(uid, me_id)] = time.time() + 15  # not "owner activity"
        await client.send_message(
            "me",
            "👻 Ghost test: your auto-reply engine is alive.\n\nThis is what your contacts receive:\n\n"
            + (settings.get("default_reply") or ""),
        )
        _last_self_send[uid] = time.time()
        await db.log_decision(uid, "test_ok", "sent_to_saved_messages")
    except DEAD_SESSION_ERRORS:
        await db.log_decision(uid, "test_failed", "session_invalid")
        await _drop_dead_session(uid)
    except Exception as exc:
        log.exception("Test reply failed for user %s", uid)
        await db.log_decision(uid, "test_failed", exc.__class__.__name__)


async def command_loop() -> None:
    while True:
        if _engine_active:
            try:
                for cmd in await db.claim_engine_commands(10):
                    await _run_command(cmd)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Command loop failed")
        await asyncio.sleep(2)


# ------------------------------------------------------------------ expiry cleanup + notifications (every 10 min)
async def _send_bot_message(user_id: int, text: str, button: Optional[str] = None, tab: str = "plans") -> bool:
    """Bot message without aiogram, so the Kaggle worker stays dependency-light."""
    global _http
    if not config.BOT_TOKEN:
        return False
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    payload: dict = {"chat_id": user_id, "parse_mode": "HTML", "text": text}
    if button and config.WEBAPP_URL:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button, "web_app": {"url": f"{config.WEBAPP_URL}/?tab={tab}"}}]]
        }
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    for _ in range(2):
        try:
            resp = await _http.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("Bot message failed: %s", exc.__class__.__name__)
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


async def _notify_expired_http(user_id: int, plan: str) -> bool:
    label = "trial" if plan == "trial" else "subscription"
    return await _send_bot_message(
        user_id,
        f"<b>Your Ghost has been paused.</b>\n\nYour {label} has expired and auto-replies are switched off. "
        "Resume instantly in one tap, your settings are saved.",
        "Pay ₹49/mo to Resume Instantly", "plans",
    )


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

    if config.BOT_TOKEN:  # "renew now" reminder ~24h before a paid plan ends
        for row in await db.list_expiring_unwarned(24):
            uid = row["telegram_user_id"]
            hours = max(1, int(row["hours_left"]))
            await _send_bot_message(
                uid,
                f"<b>Heads up!</b> Your {row['plan'].title()} plan ends in about {hours}h. "
                "Renew now and Ghost never goes offline.",
                "Renew Now", "plans",
            )
            await db.mark_expiry_warned(uid)
            await asyncio.sleep(0.05)

    for table in (_last_reply,):
        for key in [k for k, ts in table.items() if now - ts > 3700]:
            table.pop(key, None)
    for table in (_owner_replied, _bot_sending):
        for key in [k for k, ts in table.items() if now - ts > 7200]:
            table.pop(key, None)
    for key in [k for k, ts in _diag_last.items() if now - ts > 3600]:
        _diag_last.pop(key, None)
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


# ------------------------------------------------------------------ WhatsApp bridge (Node / Baileys)
async def _pipe_logs(proc) -> None:
    try:
        async for line in proc.stdout:
            log.info("[wa-bridge] %s", line.decode(errors="replace").rstrip())
    except Exception:
        pass


async def _npm_install(npm: str) -> bool:
    log.info("Installing WhatsApp bridge dependencies (first run, 1-2 minutes)...")
    try:
        proc = await asyncio.create_subprocess_exec(
            npm, "install", "--omit=dev", "--no-audit", "--no-fund", cwd=str(BRIDGE_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 420)
    except Exception as exc:
        log.warning("npm install failed to run: %s", exc.__class__.__name__)
        return False
    if proc.returncode != 0:
        log.warning("npm install failed: %s", out.decode(errors="replace")[-400:])
        return False
    return True


async def _wa_supervisor() -> None:
    """Runs the WhatsApp bridge ONLY while this process holds the engine lease (=> no duplicate replies)."""
    if not config.WA_BRIDGE_ENABLED:
        return
    entry = BRIDGE_DIR / "index.js"
    node, npm = shutil.which("node"), shutil.which("npm")
    if not entry.is_file():
        log.info("wa_bridge/index.js not found: WhatsApp bridge disabled")
        return
    if not node or not npm:
        log.warning("Node.js not found: WhatsApp bridge disabled (Telegram auto-reply is unaffected)")
        return
    installed = (BRIDGE_DIR / "node_modules").is_dir()
    proc = None
    started_at = 0.0
    backoff = 5.0
    try:
        while True:
            if _engine_active:
                if not installed:
                    installed = await _npm_install(npm)
                    if not installed:
                        await asyncio.sleep(60)
                        continue
                if proc is None or proc.returncode is not None:
                    if proc is not None:
                        log.warning("WhatsApp bridge exited (code %s), restarting in %ss", proc.returncode, int(backoff))
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 120)
                    proc = await asyncio.create_subprocess_exec(
                        node, "index.js", cwd=str(BRIDGE_DIR), env=os.environ.copy(),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    )
                    started_at = time.monotonic()
                    asyncio.create_task(_pipe_logs(proc))
                    log.info("WhatsApp bridge started (pid %s)", proc.pid)
                elif time.monotonic() - started_at > 300:
                    backoff = 5.0
            elif proc is not None and proc.returncode is None:
                log.info("Engine lease not held: stopping WhatsApp bridge")
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 10)
                except asyncio.TimeoutError:
                    proc.kill()
                proc = None
            await asyncio.sleep(2)
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 8)
            except Exception:
                proc.kill()


# ------------------------------------------------------------------ engine + entrypoints
async def run_engine(notifier: Optional[Callable[[int, str], Awaitable[bool]]] = None) -> None:
    """Lease keeper + Neon sync + expiry cleanup. Used by app.py (embedded) and main() (standalone)."""
    if notifier:
        set_notifier(notifier)
    tasks = [
        asyncio.create_task(_lease_loop(), name="lease"),
        asyncio.create_task(sync_loop(), name="sync"),
        asyncio.create_task(cleanup_loop(), name="cleanup"),
        asyncio.create_task(_wa_supervisor(), name="wa-bridge"),
        asyncio.create_task(command_loop(), name="commands"),
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
