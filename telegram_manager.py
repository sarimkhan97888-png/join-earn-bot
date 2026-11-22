"""
Profitix Ghost - Telegram OTP login manager.

Pending logins live in an in-memory TTL cache keyed by f"{user_id}:{phone}" so the
(client, phone_code_hash) pair never drops between send-code -> verify-code -> verify-2fa.
After a successful login the live client is handed over to the 24/7 worker.
"""
import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional

from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    RPCError,
    SessionPasswordNeededError,
)

import config
import database as db
import worker

log = logging.getLogger("telegram_manager")

MIN_RESEND_GAP = 25  # seconds; the UI enforces 60s, this is the server-side guard


class TGError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retry_after: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after


@dataclass
class Pending:
    client: object
    phone_code_hash: str
    phone: str
    created: float
    needs_2fa: bool = False


_pending: Dict[str, Pending] = {}
_user_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ------------------------------------------------------------------ helpers
def normalize_phone(raw: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", raw or "")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    if not re.fullmatch(r"\+\d{8,15}", cleaned):
        raise TGError("invalid_phone", "Enter a valid phone number with country code, e.g. +919876543210.")
    return cleaned


def _key(user_id: int, phone: str) -> str:
    return f"{user_id}:{phone}"


def _map_error(exc: Exception) -> TGError:
    if isinstance(exc, (FloodWaitError, PhoneNumberFloodError)):
        secs = int(getattr(exc, "seconds", 0) or 0)
        wait = f" Please wait {secs} seconds and try again." if secs else " Please try again later."
        return TGError("flood_wait", "Too many attempts." + wait, 429, secs or None)
    if isinstance(exc, PhoneNumberInvalidError):
        return TGError("invalid_phone", "That phone number is invalid. Include the country code.")
    if isinstance(exc, PhoneNumberBannedError):
        return TGError("phone_banned", "This phone number is banned by Telegram.", 403)
    if isinstance(exc, (PhoneCodeInvalidError, PhoneCodeEmptyError)):
        return TGError("invalid_code", "Wrong code. Check your official Telegram app chat and try again.")
    if isinstance(exc, PhoneCodeExpiredError):
        return TGError("code_expired", "The code expired. Tap Resend to get a new one.")
    if isinstance(exc, PasswordHashInvalidError):
        return TGError("wrong_password", "Incorrect 2FA password. Please try again.")
    if isinstance(exc, PhoneNumberUnoccupiedError):
        return TGError("not_registered", "This number is not registered on Telegram.")
    if isinstance(exc, ApiIdInvalidError):
        return TGError("server_config", "Server Telegram API credentials are invalid.", 500)
    if isinstance(exc, RPCError):
        return TGError("telegram_error", f"Telegram error: {exc.__class__.__name__}", 502)
    return TGError("server_error", "Something went wrong. Please try again.", 500)


async def _safe_disconnect(client) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


async def _drop_user_pending(user_id: int) -> None:
    prefix = f"{user_id}:"
    for key in [k for k in _pending if k.startswith(prefix)]:
        item = _pending.pop(key, None)
        if item:
            await _safe_disconnect(item.client)


async def _require_active_plan(user_id: int) -> dict:
    status = await db.get_status(user_id)
    if status["status"] != "active":
        raise TGError("no_plan", "Activate a plan first to connect your Telegram account.", 402)
    return status


# ------------------------------------------------------------------ public API
async def send_code(user_id: int, raw_phone: str) -> dict:
    phone = normalize_phone(raw_phone)
    await _require_active_plan(user_id)
    if not config.API_ID or not config.API_HASH:
        raise TGError("server_config", "Telegram API credentials are not configured.", 500)

    async with _user_locks[user_id]:
        existing = _pending.get(_key(user_id, phone))
        if existing and time.time() - existing.created < MIN_RESEND_GAP:
            raise TGError("too_fast", "Please wait a few seconds before requesting another code.", 429)

        await _drop_user_pending(user_id)
        client = worker.new_client()
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception as exc:
            await _safe_disconnect(client)
            err = _map_error(exc)
            if err.code in ("server_error", "telegram_error"):
                log.exception("send_code failed for user %s", user_id)
            raise err from exc

        _pending[_key(user_id, phone)] = Pending(client, sent.phone_code_hash, phone, time.time())
    return {"status": "code_sent", "phone": phone, "expires_in": config.OTP_CACHE_TTL_SECONDS, "resend_after": 60}


async def verify_code(user_id: int, raw_phone: str, code: str) -> dict:
    phone = normalize_phone(raw_phone)
    digits = re.sub(r"\D", "", code or "")
    if not 5 <= len(digits) <= 6:
        raise TGError("invalid_code", "Enter the 5-digit code from your Telegram app.")

    async with _user_locks[user_id]:
        pending = _pending.get(_key(user_id, phone))
        if not pending:
            raise TGError("code_expired", "Session expired. Please request a new code.", 410)
        try:
            await pending.client.sign_in(phone=phone, code=digits, phone_code_hash=pending.phone_code_hash)
        except SessionPasswordNeededError:
            pending.needs_2fa = True
            return {"status": "2fa_required"}
        except Exception as exc:
            err = _map_error(exc)
            if err.code in ("server_error", "telegram_error"):
                log.exception("verify_code failed for user %s", user_id)
            raise err from exc
        return await _finalize(user_id, phone, pending)


async def verify_2fa(user_id: int, raw_phone: str, password: str) -> dict:
    phone = normalize_phone(raw_phone)
    if not password:
        raise TGError("wrong_password", "Enter your Telegram 2FA password.")

    async with _user_locks[user_id]:
        pending = _pending.get(_key(user_id, phone))
        if not pending or not pending.needs_2fa:
            raise TGError("code_expired", "Session expired. Please start again.", 410)
        try:
            await pending.client.sign_in(password=password)
        except Exception as exc:
            err = _map_error(exc)
            if err.code in ("server_error", "telegram_error"):
                log.exception("verify_2fa failed for user %s", user_id)
            raise err from exc
        return await _finalize(user_id, phone, pending)


async def _finalize(user_id: int, phone: str, pending: Pending) -> dict:
    client = pending.client
    me = await client.get_me()
    status = await db.get_status(user_id)

    # Anti-abuse: a Telegram account that already burned a trial for another user cannot reuse it.
    if status["plan"] == "trial":
        blocked = await db.is_tg_account_trial_blocked(user_id, me.id)
        if blocked or not await db.bind_tg_account_to_trial(user_id, me.id):
            _pending.pop(_key(user_id, phone), None)
            try:
                await client.log_out()
            except Exception:
                await _safe_disconnect(client)
            raise TGError(
                "trial_account_used",
                "This Telegram account has already used the Rs.2 trial. Please choose the Starter plan.",
                403,
            )

    name = " ".join(p for p in (me.first_name, me.last_name) if p) or "Telegram User"
    username = me.username or ""
    await db.save_tg_session(user_id, client.session.save(), me.id, name, username, phone)
    _pending.pop(_key(user_id, phone), None)

    started = await worker.start_user_client(user_id, client)
    return {
        "status": "connected",
        "monitoring": started,
        "account": {"name": name, "username": username, "phone": phone, "tg_account_id": me.id},
    }


async def disconnect_account(user_id: int) -> None:
    await worker.stop_user_client(user_id, log_out=True)
    await db.delete_tg_session(user_id)
    async with _user_locks[user_id]:
        await _drop_user_pending(user_id)


async def get_account(user_id: int) -> Optional[dict]:
    row = await db.get_tg_session(user_id)
    if not row:
        return None
    return {
        "name": row["tg_name"] or "Telegram User",
        "username": row["tg_username"] or "",
        "phone": row["tg_phone"] or "",
        "tg_account_id": row["tg_account_id"],
    }


# ------------------------------------------------------------------ TTL cache maintenance
async def purge_expired() -> int:
    now = time.time()
    stale = [k for k, p in _pending.items() if now - p.created > config.OTP_CACHE_TTL_SECONDS]
    for key in stale:
        item = _pending.pop(key, None)
        if item:
            await _safe_disconnect(item.client)
    return len(stale)


async def gc_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await purge_expired()
        except Exception:
            log.exception("OTP cache purge failed")


async def shutdown() -> None:
    for key in list(_pending):
        item = _pending.pop(key, None)
        if item:
            await _safe_disconnect(item.client)
