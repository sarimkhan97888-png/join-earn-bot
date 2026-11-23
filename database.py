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
    "cooldown_seconds", "delay_min", "delay_max", "only_when_offline",
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

ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
ALTER TABLE auto_reply_settings ADD COLUMN IF NOT EXISTS only_when_offline BOOLEAN NOT NULL DEFAULT TRUE;

-- Every user action (QR generated, payment ok/failed, connect, expiry...) for the admin live feed
CREATE TABLE IF NOT EXISTS activity_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT,
    kind        TEXT NOT NULL,
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_kind ON activity_log (kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users (last_seen_at DESC);

-- Single-engine lease: guarantees only ONE auto-reply worker (Render OR Kaggle) is live => no duplicate replies
CREATE TABLE IF NOT EXISTS worker_lease (
    name            TEXT PRIMARY KEY,
    holder          TEXT NOT NULL,
    active_clients  INTEGER NOT NULL DEFAULT 0,
    expires_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS expiry_warned BOOLEAN NOT NULL DEFAULT FALSE;

-- WhatsApp bridge (Pro): session state, Baileys auth keys and a command queue (Render -> engine)
CREATE TABLE IF NOT EXISTS wa_sessions (
    user_id       BIGINT PRIMARY KEY REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'none',
    phone         TEXT,
    pairing_code  TEXT,
    wa_number     TEXT,
    wa_name       TEXT,
    error         TEXT,
    connected_at  TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS wa_auth (
    user_id  BIGINT NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS wa_commands (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    cmd         TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wa_commands_pending ON wa_commands (status, id);

-- Webhooks (Pro)
CREATE TABLE IF NOT EXISTS integrations (
    user_id         BIGINT PRIMARY KEY REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    webhook_url     TEXT,
    webhook_secret  TEXT,
    webhook_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    webhook_reply   BOOLEAN NOT NULL DEFAULT FALSE,
    last_status     INTEGER,
    last_error      TEXT,
    last_at         TIMESTAMPTZ,
    delivered       BIGINT NOT NULL DEFAULT 0,
    failed          BIGINT NOT NULL DEFAULT 0
);
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
        async with conn.transaction():
            # Render and Kaggle may boot together: serialise schema creation
            await conn.execute("SELECT pg_advisory_xact_lock(727274)")
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
                    plan_expires_at = $4, expiry_notified = FALSE, expiry_warned = FALSE, updated_at = NOW()
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


# ------------------------------------------------------------------ presence + activity log
_TODAY = "(date_trunc('day', NOW() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata')"


def _mask_phone(phone: Optional[str]) -> str:
    if not phone:
        return ""
    return phone[:3] + "•" * max(0, len(phone) - 6) + phone[-3:]


async def touch_user(user_id: int) -> None:
    """Heartbeat: marks the user as 'online now' (throttled to one write per 20s)."""
    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE users SET last_seen_at = NOW()
            WHERE telegram_user_id = $1
              AND (last_seen_at IS NULL OR last_seen_at < NOW() - INTERVAL '20 seconds')
            """,
            user_id,
        )


async def log_event(user_id: Optional[int], kind: str, meta: Optional[dict] = None) -> None:
    """Never raises: activity logging must not break a user request."""
    try:
        async with pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO activity_log (user_id, kind, meta) VALUES ($1, $2, $3)",
                user_id, kind, meta or {},
            )
    except Exception:
        log.warning("activity log write failed (%s)", kind)


async def prune_activity(days: int = 30) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM activity_log WHERE created_at < NOW() - make_interval(days => $1::int)", days
        )


# ------------------------------------------------------------------ admin panel queries
async def admin_overview() -> dict:
    async with pool().acquire() as conn:
        u = await conn.fetchrow(
            f"""
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE created_at >= {_TODAY}) AS new_today,
              COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days') AS new_7d,
              COUNT(*) FILTER (WHERE plan_status = 'active' AND plan_expires_at > NOW()) AS active_total,
              COUNT(*) FILTER (WHERE plan_status = 'active' AND plan_expires_at > NOW() AND plan = 'trial') AS active_trial,
              COUNT(*) FILTER (WHERE plan_status = 'active' AND plan_expires_at > NOW() AND plan = 'starter') AS active_starter,
              COUNT(*) FILTER (WHERE plan_status = 'active' AND plan_expires_at > NOW() AND plan = 'pro') AS active_pro,
              COUNT(*) FILTER (WHERE plan_status = 'expired' OR (plan_status = 'active' AND plan_expires_at <= NOW())) AS expired,
              COUNT(*) FILTER (WHERE plan_status = 'none') AS never_paid,
              COUNT(*) FILTER (WHERE plan_status = 'active' AND plan_expires_at > NOW()
                               AND plan_expires_at < NOW() + INTERVAL '24 hours') AS expiring_24h,
              COUNT(*) FILTER (WHERE last_seen_at > NOW() - INTERVAL '90 seconds') AS online_now,
              COUNT(*) FILTER (WHERE last_seen_at >= {_TODAY}) AS seen_today,
              COALESCE(SUM(total_replies_sent), 0) AS replies_total
            FROM users
            """
        )
        c = await conn.fetchrow(
            """
            SELECT
              COUNT(*) AS connected,
              COUNT(*) FILTER (WHERE u.plan_status = 'active' AND u.plan_expires_at > NOW()
                               AND COALESCE(a.is_active, TRUE)) AS reply_on,
              COUNT(*) FILTER (WHERE NOT COALESCE(a.is_active, TRUE)) AS reply_off,
              COUNT(*) FILTER (WHERE COALESCE(a.only_when_offline, TRUE)) AS smart_mode
            FROM tg_sessions s
            JOIN users u ON u.telegram_user_id = s.user_id
            LEFT JOIN auto_reply_settings a ON a.user_id = s.user_id
            """
        )
        money = await conn.fetch(
            f"""
            SELECT currency,
                   COALESCE(SUM(amount) FILTER (WHERE created_at >= {_TODAY}), 0) AS today,
                   COALESCE(SUM(amount), 0) AS total,
                   COUNT(*) AS n
            FROM payments WHERE status = 'success' GROUP BY currency
            """
        )
        act = await conn.fetch(
            f"""
            SELECT kind, COUNT(*) AS n, COUNT(DISTINCT user_id) AS users
            FROM activity_log WHERE created_at >= {_TODAY} GROUP BY kind
            """
        )
    async with pool().acquire() as conn2:
        x = await conn2.fetchrow(
            """
            SELECT (SELECT COUNT(*) FROM wa_sessions WHERE status = 'connected') AS wa_connected,
                   (SELECT COUNT(*) FROM integrations WHERE webhook_enabled AND webhook_url IS NOT NULL) AS webhooks_on
            """
        )
    return {
        "users": {k: int(v) for k, v in dict(u).items()},
        "accounts": {**{k: int(v) for k, v in dict(c).items()}, **{k: int(v) for k, v in dict(x).items()}},
        "money": {
            r["currency"]: {"today": float(r["today"]), "total": float(r["total"]), "count": int(r["n"])}
            for r in money
        },
        "today": {r["kind"]: {"n": int(r["n"]), "users": int(r["users"])} for r in act},
    }


async def admin_feed(after_id: int = 0, limit: int = 40) -> list:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.user_id, a.kind, a.meta, a.created_at, u.first_name, u.username
            FROM activity_log a
            LEFT JOIN users u ON u.telegram_user_id = a.user_id
            WHERE a.id > $1
            ORDER BY a.id DESC LIMIT $2
            """,
            after_id, limit,
        )
    out = []
    for r in rows:
        d = dict(r)
        d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    return out


async def admin_users(q: str = "", limit: int = 30, offset: int = 0) -> dict:
    q = (q or "").strip()
    where = """
        ($1 = '' OR u.first_name ILIKE '%' || $1 || '%' OR u.username ILIKE '%' || $1 || '%'
         OR u.telegram_user_id::text = $1)
    """
    async with pool().acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM users u WHERE {where}", q)
        rows = await conn.fetch(
            f"""
            SELECT u.telegram_user_id, u.first_name, u.username, u.plan, u.plan_status, u.plan_expires_at,
                   u.total_replies_sent, u.trial_used, u.created_at, u.last_seen_at,
                   s.tg_phone, s.tg_name, COALESCE(a.is_active, TRUE) AS reply_on,
                   COALESCE(a.only_when_offline, TRUE) AS smart_mode
            FROM users u
            LEFT JOIN tg_sessions s ON s.user_id = u.telegram_user_id
            LEFT JOIN auto_reply_settings a ON a.user_id = u.telegram_user_id
            WHERE {where}
            ORDER BY u.last_seen_at DESC NULLS LAST, u.created_at DESC
            LIMIT $2 OFFSET $3
            """,
            q, limit, offset,
        )
    now = _now()
    items = []
    for r in rows:
        d = dict(r)
        exp = d["plan_expires_at"]
        if d["plan_status"] == "active" and exp and exp > now:
            d["status"] = "active"
        elif d["plan_status"] in ("active", "expired"):
            d["status"] = "expired"
        else:
            d["status"] = "none"
        d["connected"] = d["tg_phone"] is not None
        d["tg_phone"] = _mask_phone(d["tg_phone"])
        d["online"] = bool(d["last_seen_at"] and (now - d["last_seen_at"]).total_seconds() < 90)
        for key in ("plan_expires_at", "created_at", "last_seen_at"):
            d[key] = d[key].isoformat() if d[key] else None
        items.append(d)
    return {"total": int(total), "users": items}


async def admin_payments(limit: int = 30) -> list:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.user_id, p.plan_key, p.amount, p.currency, p.method, p.status, p.created_at,
                   u.first_name, u.username
            FROM payments p LEFT JOIN users u ON u.telegram_user_id = p.user_id
            ORDER BY p.created_at DESC LIMIT $1
            """,
            limit,
        )
    out = []
    for r in rows:
        d = dict(r)
        d["amount"] = float(d["amount"])
        d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    return out


# ------------------------------------------------------------------ webhooks (Pro)
_EMPTY_HOOK = {
    "webhook_url": "", "webhook_secret": "", "webhook_enabled": False, "webhook_reply": False,
    "last_status": None, "last_error": None, "last_at": None, "delivered": 0, "failed": 0,
}


async def get_integration(user_id: int) -> dict:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT webhook_url, webhook_secret, webhook_enabled, webhook_reply,
                   last_status, last_error, last_at, delivered, failed
            FROM integrations WHERE user_id = $1
            """,
            user_id,
        )
    return dict(row) if row else dict(_EMPTY_HOOK)


async def save_integration(user_id: int, url: str, secret: str, enabled: bool, reply: bool) -> dict:
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO integrations (user_id, webhook_url, webhook_secret, webhook_enabled, webhook_reply)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE
            SET webhook_url = $2, webhook_secret = $3, webhook_enabled = $4, webhook_reply = $5
            """,
            user_id, url, secret, enabled, reply,
        )
    return await get_integration(user_id)


async def webhook_result(user_id: int, ok: bool, status: Optional[int], error: str) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE integrations
            SET delivered = delivered + $2, failed = failed + $3,
                last_status = $4, last_error = $5, last_at = NOW()
            WHERE user_id = $1
            """,
            user_id, 1 if ok else 0, 0 if ok else 1, status, (error or "")[:200],
        )


# ------------------------------------------------------------------ whatsapp bridge (Pro)
async def get_wa_session(user_id: int) -> Optional[dict]:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT status, phone, pairing_code, wa_number, wa_name, error, connected_at,
                   EXTRACT(EPOCH FROM (NOW() - updated_at))::float AS age
            FROM wa_sessions WHERE user_id = $1
            """,
            user_id,
        )
    if not row:
        return None
    d = dict(row)
    d["connected_at"] = d["connected_at"].isoformat() if d["connected_at"] else None
    return d


async def wa_request_pair(user_id: int, phone: str) -> None:
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO wa_sessions (user_id, status, phone, pairing_code, error)
                VALUES ($1, 'pairing', $2, NULL, NULL)
                ON CONFLICT (user_id) DO UPDATE
                SET status = 'pairing', phone = $2, pairing_code = NULL, error = NULL, updated_at = NOW()
                """,
                user_id, phone,
            )
            await conn.execute(
                "INSERT INTO wa_commands (user_id, cmd, payload) VALUES ($1, 'pair', $2)",
                user_id, {"phone": phone},
            )


async def wa_request_logout(user_id: int) -> None:
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO wa_commands (user_id, cmd, payload) VALUES ($1, 'logout', '{}'::jsonb)", user_id
            )
            await conn.execute(
                """
                UPDATE wa_sessions SET status = 'none', pairing_code = NULL, error = NULL, updated_at = NOW()
                WHERE user_id = $1
                """,
                user_id,
            )


# ------------------------------------------------------------------ retention + ops helpers
async def list_expiring_unwarned(hours: int = 24) -> list:
    """Paid plans ending within `hours` that have not received the 'renew now' reminder yet."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT telegram_user_id, plan,
                   EXTRACT(EPOCH FROM (plan_expires_at - NOW())) / 3600.0 AS hours_left
            FROM users
            WHERE plan_status = 'active' AND plan <> 'trial' AND expiry_warned = FALSE
              AND plan_expires_at > NOW() AND plan_expires_at < NOW() + make_interval(hours => $1::int)
            """,
            hours,
        )
    return [dict(r) for r in rows]


async def mark_expiry_warned(user_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute("UPDATE users SET expiry_warned = TRUE WHERE telegram_user_id = $1", user_id)


async def count_live_users() -> int:
    """Paying users that currently depend on the auto-reply engine (Telegram or WhatsApp linked)."""
    async with pool().acquire() as conn:
        return int(await conn.fetchval(
            """
            SELECT COUNT(*) FROM users u
            WHERE u.plan_status = 'active' AND u.plan_expires_at > NOW()
              AND (EXISTS (SELECT 1 FROM tg_sessions s WHERE s.user_id = u.telegram_user_id)
                   OR EXISTS (SELECT 1 FROM wa_sessions w WHERE w.user_id = u.telegram_user_id AND w.status = 'connected'))
            """
        ))
