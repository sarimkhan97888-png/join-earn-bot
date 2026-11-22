"""
Profitix Ghost - PostgreSQL (Neon.tech) data layer using asyncpg.
Only counters are stored for replies (no message text logs) to keep the DB tiny.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import asyncpg

import config

log = logging.getLogger("database")
_pool: Optional[asyncpg.Pool] = None

SETTINGS_FIELDS = {
    "is_active", "template", "default_reply", "keyword_rules",
    "cooldown_seconds", "delay_min", "delay_max",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id    BIGINT PRIMARY KEY,
    first_name          TEXT,
    username            TEXT,
    plan                TEXT NOT NULL DEFAULT 'none',
    billing_period      TEXT,
    plan_status         TEXT NOT NULL DEFAULT 'none',
    plan_expires_at     TIMESTAMPTZ,
    trial_used          BOOLEAN NOT NULL DEFAULT FALSE,
    total_replies_sent  BIGINT NOT NULL DEFAULT 0,
    expiry_notified     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tg_sessions (
    user_id         BIGINT PRIMARY KEY REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    session_string  TEXT NOT NULL,
    tg_account_id   BIGINT NOT NULL,
    tg_name         TEXT,
    tg_username     TEXT,
    tg_phone        TEXT,
    connected_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auto_reply_settings (
    user_id           BIGINT PRIMARY KEY REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    template          TEXT NOT NULL DEFAULT 'busy',
    default_reply     TEXT NOT NULL DEFAULT 'Hi! I am currently busy and will get back to you as soon as I can.',
    keyword_rules     JSONB NOT NULL DEFAULT '[]'::jsonb,
    cooldown_seconds  INTEGER NOT NULL DEFAULT 600,
    delay_min         INTEGER NOT NULL DEFAULT 2,
    delay_max         INTEGER NOT NULL DEFAULT 5,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Triple anti-abuse lock for the Rs.2 trial. Each identifier is UNIQUE on its own,
-- so the same Telegram user, the same payer UPI hash, or the same connected
-- Telegram account can never claim the trial twice.
CREATE TABLE IF NOT EXISTS trial_locks (
    id                       SERIAL PRIMARY KEY,
    telegram_user_id         BIGINT UNIQUE,
    payer_upi_hash           TEXT UNIQUE,
    connected_tg_account_id  BIGINT UNIQUE,
    trial_used               BOOLEAN NOT NULL DEFAULT FALSE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Replay protection: UTR (UPI) or tx_hash (USDT) can be redeemed exactly once.
CREATE TABLE IF NOT EXISTS used_payments (
    ref_id      TEXT PRIMARY KEY,
    method      TEXT NOT NULL,
    user_id     BIGINT NOT NULL,
    plan_key    TEXT NOT NULL,
    amount      NUMERIC(18,2) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payments (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    plan_key    TEXT NOT NULL,
    amount      NUMERIC(18,2) NOT NULL,
    currency    TEXT NOT NULL,
    method      TEXT NOT NULL,
    status      TEXT NOT NULL,
    ref_id      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_status_expiry ON users (plan_status, plan_expires_at);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tg_sessions_account ON tg_sessions (tg_account_id);
CREATE INDEX IF NOT EXISTS idx_trial_locks_flags ON trial_locks (trial_used);
"""


# ------------------------------------------------------------------ pool
def _clean_dsn(url: str) -> str:
    """Neon URLs carry ?sslmode=require&channel_binding=require which asyncpg would
    treat as unknown server settings. SSL is passed explicitly instead."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def init_db() -> None:
    global _pool
    if _pool is not None:
        return
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    _pool = await asyncpg.create_pool(
        dsn=_clean_dsn(config.DATABASE_URL),
        ssl="require",
        min_size=1,
        max_size=8,
        command_timeout=30,
        statement_cache_size=0,  # required for Neon's PgBouncer pooler
        init=_init_conn,
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
    log.info("Database ready")


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _d(row: Optional[asyncpg.Record]) -> Optional[dict]:
    return dict(row) if row else None


# ------------------------------------------------------------------ users
async def upsert_user(user_id: int, first_name: str = "", username: str = "") -> dict:
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO users (telegram_user_id, first_name, username)
                VALUES ($1, $2, $3)
                ON CONFLICT (telegram_user_id) DO UPDATE
                SET first_name = COALESCE(NULLIF($2, ''), users.first_name),
                    username   = COALESCE(NULLIF($3, ''), users.username),
                    updated_at = NOW()
                RETURNING *
                """,
                user_id, first_name or "", username or "",
            )
            await conn.execute(
                "INSERT INTO auto_reply_settings (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                user_id,
            )
    return dict(row)


async def get_user(user_id: int) -> Optional[dict]:
    async with pool().acquire() as conn:
        return _d(await conn.fetchrow("SELECT * FROM users WHERE telegram_user_id = $1", user_id))


async def get_status(user_id: int) -> dict:
    """Effective subscription state (expiry is evaluated live, not only by the cleanup job)."""
    user = await get_user(user_id)
    if not user:
        return {
            "plan": "none", "billing_period": None, "status": "none", "expires_at": None,
            "seconds_left": 0, "trial_used": False, "replies_sent": 0,
        }
    expires = user["plan_expires_at"]
    seconds_left = max(0, int((expires - _now()).total_seconds())) if expires else 0
    status = user["plan_status"]
    if status == "active" and seconds_left <= 0:
        status = "expired"
    return {
        "plan": user["plan"],
        "billing_period": user["billing_period"],
        "status": status,
        "expires_at": expires.isoformat() if expires else None,
        "seconds_left": seconds_left if status == "active" else 0,
        "trial_used": bool(user["trial_used"]),
        "replies_sent": int(user["total_replies_sent"]),
    }


async def activate_plan(user_id: int, tier: str, period: str, days: int) -> datetime:
    """Activates / extends a plan. Paid renewals stack on top of remaining time."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO users (telegram_user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
            )
            row = await conn.fetchrow(
                "SELECT plan_status, plan_expires_at FROM users WHERE telegram_user_id = $1 FOR UPDATE",
                user_id,
            )
            base = _now()
            if (
                tier != "trial"
                and row["plan_status"] == "active"
                and row["plan_expires_at"]
                and row["plan_expires_at"] > base
            ):
                base = row["plan_expires_at"]
            new_expiry = base + timedelta(days=days)
            await conn.execute(
                """
                UPDATE users
                SET plan = $2, billing_period = $3, plan_status = 'active',
                    plan_expires_at = $4, expiry_notified = FALSE, updated_at = NOW()
                WHERE telegram_user_id = $1
                """,
                user_id, tier, period, new_expiry,
            )
    return new_expiry


async def mark_expired(user_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET plan_status = 'expired', updated_at = NOW() WHERE telegram_user_id = $1",
            user_id,
        )


async def mark_expiry_notified(user_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET expiry_notified = TRUE WHERE telegram_user_id = $1", user_id
        )


async def list_due_for_expiry() -> list:
    """Users flagged active whose time is up (used by the 10-minute cleanup job)."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT telegram_user_id, plan, expiry_notified FROM users
            WHERE plan_status = 'active' AND plan_expires_at <= NOW()
            """
        )
    return [dict(r) for r in rows]


async def list_expired_unnotified() -> list:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT telegram_user_id, plan FROM users WHERE plan_status = 'expired' AND expiry_notified = FALSE"
        )
    return [dict(r) for r in rows]


async def increment_replies(user_id: int, n: int = 1) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET total_replies_sent = total_replies_sent + $2 WHERE telegram_user_id = $1",
            user_id, n,
        )


# ------------------------------------------------------------------ telegram sessions
async def save_tg_session(
    user_id: int, session_string: str, tg_account_id: int,
    name: str = "", username: str = "", phone: str = "",
) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tg_sessions (user_id, session_string, tg_account_id, tg_name, tg_username, tg_phone)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (user_id) DO UPDATE
            SET session_string = $2, tg_account_id = $3, tg_name = $4,
                tg_username = $5, tg_phone = $6, connected_at = NOW()
            """,
            user_id, session_string, tg_account_id, name, username, phone,
        )


async def get_tg_session(user_id: int) -> Optional[dict]:
    async with pool().acquire() as conn:
        return _d(await conn.fetchrow("SELECT * FROM tg_sessions WHERE user_id = $1", user_id))


async def delete_tg_session(user_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute("DELETE FROM tg_sessions WHERE user_id = $1", user_id)


async def list_active_sessions() -> list:
    """Sessions of users whose subscription is currently valid (worker boot / restore)."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.*, u.plan, u.plan_expires_at
            FROM tg_sessions s
            JOIN users u ON u.telegram_user_id = s.user_id
            WHERE u.plan_status = 'active' AND u.plan_expires_at > NOW()
            """
        )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ auto-reply settings
async def get_settings(user_id: int) -> dict:
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO auto_reply_settings (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
        )
        row = await conn.fetchrow("SELECT * FROM auto_reply_settings WHERE user_id = $1", user_id)
    data = dict(row)
    data.pop("updated_at", None)
    return data


async def save_settings(user_id: int, **fields: Any) -> dict:
    clean = {k: v for k, v in fields.items() if k in SETTINGS_FIELDS and v is not None}
    if clean:
        sets, values = [], [user_id]
        for i, (k, v) in enumerate(clean.items(), start=2):
            sets.append(f"{k} = ${i}")
            values.append(v)
        async with pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO auto_reply_settings (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
            )
            await conn.execute(
                f"UPDATE auto_reply_settings SET {', '.join(sets)}, updated_at = NOW() WHERE user_id = $1",
                *values,
            )
    return await get_settings(user_id)


# ------------------------------------------------------------------ trial anti-abuse
async def trial_eligibility(
    user_id: int, payer_upi_hash: Optional[str] = None, tg_account_id: Optional[int] = None
) -> tuple:
    """Returns (eligible, reason). Checks all three identifiers."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT telegram_user_id, payer_upi_hash, connected_tg_account_id
            FROM trial_locks
            WHERE trial_used = TRUE
              AND (telegram_user_id = $1
                   OR ($2::text IS NOT NULL AND payer_upi_hash = $2)
                   OR ($3::bigint IS NOT NULL AND connected_tg_account_id = $3))
            LIMIT 1
            """,
            user_id, payer_upi_hash, tg_account_id,
        )
        if row:
            if row["telegram_user_id"] == user_id:
                return False, "telegram_user"
            if payer_upi_hash and row["payer_upi_hash"] == payer_upi_hash:
                return False, "payer_upi"
            return False, "tg_account"
        user = await conn.fetchrow(
            "SELECT trial_used FROM users WHERE telegram_user_id = $1", user_id
        )
        if user and user["trial_used"]:
            return False, "telegram_user"
    return True, ""


async def lock_trial(user_id: int, payer_upi_hash: Optional[str]) -> bool:
    """Atomically burns the trial for this Telegram user + payer. False => already abused."""
    try:
        async with pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
                exists = await conn.fetchval(
                    """
                    SELECT 1 FROM trial_locks
                    WHERE trial_used = TRUE
                      AND (telegram_user_id = $1
                           OR ($2::text IS NOT NULL AND payer_upi_hash = $2))
                    LIMIT 1
                    """,
                    user_id, payer_upi_hash,
                )
                if exists:
                    return False
                await conn.execute(
                    """
                    INSERT INTO trial_locks (telegram_user_id, payer_upi_hash, trial_used)
                    VALUES ($1, $2, TRUE)
                    ON CONFLICT (telegram_user_id) DO UPDATE
                    SET payer_upi_hash = COALESCE(trial_locks.payer_upi_hash, EXCLUDED.payer_upi_hash),
                        trial_used = TRUE
                    """,
                    user_id, payer_upi_hash,
                )
                await conn.execute(
                    "UPDATE users SET trial_used = TRUE, updated_at = NOW() WHERE telegram_user_id = $1",
                    user_id,
                )
        return True
    except asyncpg.UniqueViolationError:
        return False


async def is_tg_account_trial_blocked(user_id: int, tg_account_id: int) -> bool:
    """True if this Telegram account is already tied to a trial of a DIFFERENT user."""
    async with pool().acquire() as conn:
        found = await conn.fetchval(
            """
            SELECT 1 FROM trial_locks
            WHERE connected_tg_account_id = $2 AND telegram_user_id <> $1
            LIMIT 1
            """,
            user_id, tg_account_id,
        )
    return bool(found)


async def bind_tg_account_to_trial(user_id: int, tg_account_id: int) -> bool:
    """Stores the connected Telegram account on the trial lock (first account wins)."""
    try:
        async with pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE trial_locks
                SET connected_tg_account_id = COALESCE(connected_tg_account_id, $2)
                WHERE telegram_user_id = $1
                """,
                user_id, tg_account_id,
            )
        return True
    except asyncpg.UniqueViolationError:
        return False


# ------------------------------------------------------------------ payments
async def is_payment_used(ref_id: str) -> bool:
    async with pool().acquire() as conn:
        return bool(
            await conn.fetchval("SELECT 1 FROM used_payments WHERE ref_id = $1", ref_id.lower())
        )


async def reserve_payment(ref_id: str, method: str, user_id: int, plan_key: str, amount: float) -> bool:
    """Atomic replay lock. Returns False if this UTR / tx_hash was already redeemed."""
    async with pool().acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO used_payments (ref_id, method, user_id, plan_key, amount)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (ref_id) DO NOTHING
            """,
            ref_id.lower(), method, user_id, plan_key, amount,
        )
    return result.endswith("1")


async def release_payment(ref_id: str) -> None:
    """Frees a reservation when activation fails after reserving."""
    async with pool().acquire() as conn:
        await conn.execute("DELETE FROM used_payments WHERE ref_id = $1", ref_id.lower())


async def add_payment(
    user_id: int, plan_key: str, amount: float, currency: str,
    method: str, status: str, ref_id: Optional[str] = None,
) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO payments (user_id, plan_key, amount, currency, method, status, ref_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            user_id, plan_key, amount, currency, method, status, ref_id,
        )


async def list_payments(user_id: int, limit: int = 20) -> list:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT plan_key, amount, currency, method, status, created_at
            FROM payments WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2
            """,
            user_id, limit,
        )
    out = []
    for r in rows:
        item = dict(r)
        item["amount"] = float(item["amount"])
        item["created_at"] = item["created_at"].isoformat()
        out.append(item)
    return out
