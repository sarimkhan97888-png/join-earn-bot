"""Profitix Ghost — SQLite database layer.

* SQLite + WAL + busy_timeout=5000 (FastAPI, bot aur worker ek saath chalein to lock na ho)
* Har call apna chhota connection leta hai aur thread me chalta hai -> event loop block nahi hota
* Triple anti-abuse trial lock:
    1) users.trial_used                (telegram_user_id)
    2) trial_payers.payer_hash         (UPI payer ka hash)
    3) trial_accounts.tg_account_id    (Telethon se jodi gayi asli Telegram account ID)
* Duplicate payment guard: used_utrs (UPI) aur used_tx (USDT) PRIMARY KEY se
* Telethon StringSession encrypted store hota hai (stdlib only: HMAC-SHA256 stream + MAC)
"""
import asyncio
import base64
import hashlib
import hmac
import os
import sqlite3
import time

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    telegram_user_id        INTEGER PRIMARY KEY,
    username                TEXT,
    first_name              TEXT,
    created_at              INTEGER NOT NULL,
    trial_used              INTEGER NOT NULL DEFAULT 0,
    trial_started_at        INTEGER,
    plan                    TEXT,
    billing                 TEXT,
    expires_at              INTEGER NOT NULL DEFAULT 0,
    sub_status              TEXT NOT NULL DEFAULT 'none',
    expiry_notified         INTEGER NOT NULL DEFAULT 0,
    connected_tg_account_id INTEGER,
    connected_phone         TEXT,
    session_enc             TEXT,
    autoreply_enabled       INTEGER NOT NULL DEFAULT 1,
    reply_message           TEXT NOT NULL DEFAULT 'Hi! I am away right now. I will reply to you soon.',
    webhook_url             TEXT,
    replies_sent            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rules(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    keyword     TEXT NOT NULL,
    reply       TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(telegram_user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS rules_user ON rules(user_id);

CREATE TABLE IF NOT EXISTS used_utrs(
    utr         TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    plan_key    TEXT,
    amount      REAL,
    payer_hash  TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS used_tx(
    tx_hash     TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    plan_key    TEXT,
    amount      REAL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trial_payers(
    payer_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trial_accounts(
    tg_account_id INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    method      TEXT NOT NULL,
    plan_key    TEXT NOT NULL,
    ref         TEXT NOT NULL,
    amount      REAL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS payments_user ON payments(user_id);
"""

MAX_RULES_PER_USER = 50


# --------------------------------------------------------------------------
#  Connection helpers
# --------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=5, isolation_level=None)  # autocommit, transaction hum khud
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


async def _run(fn, *args):
    def job():
        conn = _connect()
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    return await asyncio.to_thread(job)


def _row(r):
    return dict(r) if r is not None else None


def _now() -> int:
    return int(time.time())


async def init() -> None:
    """Tables banao. Har process (app/bot/worker) ke start pe safe hai."""
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    await _run(lambda c: c.executescript(SCHEMA))


# --------------------------------------------------------------------------
#  Session encryption (stdlib only; Termux pe koi extra package nahi)
# --------------------------------------------------------------------------
def _keys():
    if not config.SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET set nahi hai (.env)")
    base = hashlib.sha256(("ghost-session-v1|" + config.SESSION_SECRET).encode()).digest()
    k_enc = hmac.new(base, b"enc", hashlib.sha256).digest()
    k_mac = hmac.new(base, b"mac", hashlib.sha256).digest()
    return k_enc, k_mac


def _stream(k_enc: bytes, nonce: bytes, n: int) -> bytes:
    out, ctr = bytearray(), 0
    while len(out) < n:
        out += hmac.new(k_enc, nonce + ctr.to_bytes(8, "big"), hashlib.sha256).digest()
        ctr += 1
    return bytes(out[:n])


def encrypt_text(text: str) -> str:
    k_enc, k_mac = _keys()
    nonce = os.urandom(16)
    data = text.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _stream(k_enc, nonce, len(data))))
    tag = hmac.new(k_mac, nonce + ct, hashlib.sha256).digest()
    return "v1:" + base64.urlsafe_b64encode(nonce + ct + tag).decode()


def decrypt_text(blob: str):
    """Galat key / tampered data par None deta hai (exception nahi)."""
    try:
        if not blob or not blob.startswith("v1:"):
            return None
        raw = base64.urlsafe_b64decode(blob[3:].encode())
        nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
        k_enc, k_mac = _keys()
        if not hmac.compare_digest(tag, hmac.new(k_mac, nonce + ct, hashlib.sha256).digest()):
            return None
        return bytes(a ^ b for a, b in zip(ct, _stream(k_enc, nonce, len(ct)))).decode("utf-8")
    except Exception:
        return None


# --------------------------------------------------------------------------
#  Users
# --------------------------------------------------------------------------
async def upsert_user(uid: int, username: str = None, first_name: str = None) -> dict:
    def fn(c):
        c.execute(
            "INSERT INTO users(telegram_user_id, username, first_name, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (uid, username, first_name, _now()),
        )
        return _row(c.execute("SELECT * FROM users WHERE telegram_user_id=?", (uid,)).fetchone())

    return await _run(fn)


async def get_user(uid: int):
    return await _run(lambda c: _row(c.execute("SELECT * FROM users WHERE telegram_user_id=?", (uid,)).fetchone()))


def effective_status(u: dict, now: int = None) -> str:
    """'none' | 'trial' | 'active' | 'expired' (expiry time ke hisaab se, cleanup ka intezaar nahi)."""
    if not u:
        return "none"
    now = now or _now()
    st = u.get("sub_status") or "none"
    if st in ("trial", "active"):
        return st if (u.get("expires_at") or 0) > now else "expired"
    return st


def features_for(u: dict, now: int = None) -> dict:
    st = effective_status(u, now)
    if st in ("trial", "active") and u.get("plan") in config.TIER_FEATURES:
        return dict(config.TIER_FEATURES[u["plan"]])
    return {"telegram": False, "whatsapp": False, "webhooks": False}


async def overview(uid: int):
    """Mini App Home/Ghost/Plans tab ke liye ek hi jagah se saara state."""
    u = await get_user(uid)
    if not u:
        return None
    now = _now()
    st = effective_status(u, now)
    return {
        "user_id": uid,
        "status": st,
        "plan": u["plan"],
        "billing": u["billing"],
        "expires_at": u["expires_at"],
        "seconds_left": max(0, (u["expires_at"] or 0) - now) if st in ("trial", "active") else 0,
        "features": features_for(u, now),
        "trial_used": bool(u["trial_used"]),
        "trial_eligible": trial_eligible_row(u, now),
        "connected": bool(u["session_enc"]),
        "connected_tg_account_id": u["connected_tg_account_id"],
        "connected_phone": u["connected_phone"],
        "autoreply_enabled": bool(u["autoreply_enabled"]),
        "reply_message": u["reply_message"],
        "webhook_url": u["webhook_url"],
        "replies_sent": u["replies_sent"],
    }


async def set_autoreply(uid: int, on: bool) -> None:
    await _run(lambda c: c.execute("UPDATE users SET autoreply_enabled=? WHERE telegram_user_id=?", (1 if on else 0, uid)))


async def set_reply_message(uid: int, text: str) -> None:
    text = (text or "").strip()[:2000]
    if not text:
        raise ValueError("empty reply")
    await _run(lambda c: c.execute("UPDATE users SET reply_message=? WHERE telegram_user_id=?", (text, uid)))


async def set_webhook(uid: int, url: str) -> None:
    url = (url or "").strip()
    if url and not url.lower().startswith(("https://", "http://")):
        raise ValueError("webhook URL http(s) se shuru hona chahiye")
    await _run(lambda c: c.execute("UPDATE users SET webhook_url=? WHERE telegram_user_id=?", (url or None, uid)))


async def incr_replies(uid: int, n: int = 1) -> None:
    await _run(lambda c: c.execute("UPDATE users SET replies_sent=replies_sent+? WHERE telegram_user_id=?", (n, uid)))


# --------------------------------------------------------------------------
#  Keyword rules
# --------------------------------------------------------------------------
async def list_rules(uid: int) -> list:
    return await _run(lambda c: [dict(r) for r in c.execute("SELECT id, keyword, reply FROM rules WHERE user_id=? ORDER BY id", (uid,))])


async def add_rule(uid: int, keyword: str, reply: str) -> dict:
    keyword, reply = (keyword or "").strip().lower()[:100], (reply or "").strip()[:2000]
    if not keyword or not reply:
        return {"ok": False, "reason": "empty"}

    def fn(c):
        n = c.execute("SELECT count(*) FROM rules WHERE user_id=?", (uid,)).fetchone()[0]
        if n >= MAX_RULES_PER_USER:
            return {"ok": False, "reason": "limit"}
        cur = c.execute("INSERT INTO rules(user_id, keyword, reply, created_at) VALUES(?,?,?,?)", (uid, keyword, reply, _now()))
        return {"ok": True, "id": cur.lastrowid}

    return await _run(fn)


async def delete_rule(uid: int, rule_id: int) -> bool:
    return await _run(lambda c: c.execute("DELETE FROM rules WHERE id=? AND user_id=?", (rule_id, uid)).rowcount > 0)


# --------------------------------------------------------------------------
#  Telegram account session (Telethon StringSession)
# --------------------------------------------------------------------------
def _account_conflict(conn, account_id: int, uid: int) -> bool:
    r = conn.execute("SELECT user_id FROM trial_accounts WHERE tg_account_id=?", (account_id,)).fetchone()
    return bool(r) and r["user_id"] != uid


async def trial_account_conflict(account_id: int, uid: int) -> bool:
    """True = ye Telegram account kisi aur user ke trial me pehle use ho chuka."""
    return await _run(lambda c: _account_conflict(c, account_id, uid))


async def save_session(uid: int, string_session: str, tg_account_id: int, phone: str) -> dict:
    """Connected account save karo. Agar user trial par hai aur ye account pehle kisi aur ke trial me
    use hua tha to {'ok': False, 'reason': 'trial_account_used'} (3rd anti-abuse lock)."""
    enc = encrypt_text(string_session)

    def fn(c):
        c.execute("BEGIN IMMEDIATE")
        try:
            u = c.execute("SELECT sub_status, expires_at FROM users WHERE telegram_user_id=?", (uid,)).fetchone()
            if not u:
                c.execute("ROLLBACK")
                return {"ok": False, "reason": "no_user"}
            on_trial = u["sub_status"] == "trial" and u["expires_at"] > _now()
            if on_trial and _account_conflict(c, tg_account_id, uid):
                c.execute("ROLLBACK")
                return {"ok": False, "reason": "trial_account_used"}
            c.execute(
                "UPDATE users SET session_enc=?, connected_tg_account_id=?, connected_phone=? WHERE telegram_user_id=?",
                (enc, tg_account_id, phone, uid),
            )
            if on_trial:
                c.execute("INSERT OR IGNORE INTO trial_accounts(tg_account_id, user_id, created_at) VALUES(?,?,?)", (tg_account_id, uid, _now()))
            c.execute("COMMIT")
            return {"ok": True}
        except Exception:
            c.execute("ROLLBACK")
            raise

    return await _run(fn)


async def load_session(uid: int):
    r = await _run(lambda c: c.execute("SELECT session_enc FROM users WHERE telegram_user_id=?", (uid,)).fetchone())
    return decrypt_text(r["session_enc"]) if r and r["session_enc"] else None


async def clear_session(uid: int) -> None:
    """Disconnect. trial_accounts me record rehta hai (lock permanent)."""
    await _run(lambda c: c.execute(
        "UPDATE users SET session_enc=NULL, connected_tg_account_id=NULL, connected_phone=NULL WHERE telegram_user_id=?", (uid,)))


# --------------------------------------------------------------------------
#  Trial / payment guards
# --------------------------------------------------------------------------
def trial_eligible_row(u: dict, now: int = None) -> bool:
    if not u or u["trial_used"]:
        return False
    return effective_status(u, now) != "active"


async def trial_eligible(uid: int) -> bool:
    return trial_eligible_row(await get_user(uid))


async def utr_used(utr: str) -> bool:
    return await _run(lambda c: c.execute("SELECT 1 FROM used_utrs WHERE utr=?", (utr,)).fetchone() is not None)


async def tx_used(tx_hash: str) -> bool:
    return await _run(lambda c: c.execute("SELECT 1 FROM used_tx WHERE tx_hash=?", (tx_hash.lower(),)).fetchone() is not None)


def _apply_plan(c, uid: int, plan_key: str, now: int) -> dict:
    p = config.PLANS[plan_key]
    u = c.execute("SELECT plan, expires_at, sub_status, connected_tg_account_id FROM users WHERE telegram_user_id=?", (uid,)).fetchone()
    base = now
    # Wahi tier dobara kharido to bacha hua time aage jud jata hai.
    # (Starter -> Pro upgrade me naya plan abhi se shuru hota hai.)
    if plan_key != "trial" and u["sub_status"] == "active" and u["plan"] == p["tier"] and u["expires_at"] > now:
        base = u["expires_at"]
    expires = base + p["days"] * 86400
    status = "trial" if plan_key == "trial" else "active"
    c.execute(
        "UPDATE users SET plan=?, billing=?, expires_at=?, sub_status=?, expiry_notified=0 WHERE telegram_user_id=?",
        (p["tier"], p["period"], expires, status, uid),
    )
    if plan_key == "trial":
        c.execute("UPDATE users SET trial_used=1, trial_started_at=? WHERE telegram_user_id=?", (now, uid))
    return {"plan": p["tier"], "billing": p["period"], "expires_at": expires, "status": status}


def _fail(c, reason: str) -> dict:
    c.execute("ROLLBACK")
    return {"ok": False, "reason": reason}


def _activate(c, uid: int, plan_key: str, method: str, ref: str, amount: float, payer_hash):
    """Ek transaction me: duplicate check + trial locks + ref save + plan activate. method: 'utr' | 'tx'."""
    if plan_key not in config.PLANS:
        return {"ok": False, "reason": "bad_plan"}
    now = _now()
    c.execute("BEGIN IMMEDIATE")
    try:
        u = c.execute("SELECT * FROM users WHERE telegram_user_id=?", (uid,)).fetchone()
        if not u:
            return _fail(c, "no_user")
        table, col = ("used_utrs", "utr") if method == "utr" else ("used_tx", "tx_hash")
        if c.execute(f"SELECT 1 FROM {table} WHERE {col}=?", (ref,)).fetchone():
            return _fail(c, "duplicate_ref")

        if plan_key == "trial":
            if method != "utr":
                return _fail(c, "trial_upi_only")
            # Lock 1: telegram_user_id
            if u["trial_used"]:
                return _fail(c, "trial_used")
            if u["sub_status"] == "active" and u["expires_at"] > now:
                return _fail(c, "already_paid")
            # Lock 2: UPI payer hash
            if payer_hash and c.execute("SELECT 1 FROM trial_payers WHERE payer_hash=?", (payer_hash,)).fetchone():
                return _fail(c, "trial_payer_used")
            # Lock 3: connected Telegram account
            acc = u["connected_tg_account_id"]
            if acc and _account_conflict(c, acc, uid):
                return _fail(c, "trial_account_used")

        if method == "utr":
            c.execute("INSERT INTO used_utrs(utr, user_id, plan_key, amount, payer_hash, created_at) VALUES(?,?,?,?,?,?)",
                      (ref, uid, plan_key, amount, payer_hash, now))
        else:
            c.execute("INSERT INTO used_tx(tx_hash, user_id, plan_key, amount, created_at) VALUES(?,?,?,?,?)",
                      (ref, uid, plan_key, amount, now))
        c.execute("INSERT INTO payments(user_id, method, plan_key, ref, amount, created_at) VALUES(?,?,?,?,?,?)",
                  (uid, method, plan_key, ref, amount, now))

        res = _apply_plan(c, uid, plan_key, now)

        if plan_key == "trial":
            if payer_hash:
                c.execute("INSERT OR IGNORE INTO trial_payers(payer_hash, user_id, created_at) VALUES(?,?,?)", (payer_hash, uid, now))
            if u["connected_tg_account_id"]:
                c.execute("INSERT OR IGNORE INTO trial_accounts(tg_account_id, user_id, created_at) VALUES(?,?,?)",
                          (u["connected_tg_account_id"], uid, now))
        c.execute("COMMIT")
        res.update(ok=True, plan_key=plan_key)
        return res
    except sqlite3.IntegrityError:
        c.execute("ROLLBACK")
        return {"ok": False, "reason": "duplicate_ref"}
    except Exception:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


async def activate_utr_payment(uid: int, plan_key: str, utr: str, amount: float, payer_hash=None) -> dict:
    return await _run(_activate, uid, plan_key, "utr", utr, float(amount), payer_hash)


async def activate_tx_payment(uid: int, plan_key: str, tx_hash: str, amount: float) -> dict:
    return await _run(_activate, uid, plan_key, "tx", tx_hash.lower(), float(amount), None)


# --------------------------------------------------------------------------
#  Worker helpers (24/7 reply worker + 10-min RAM cleanup)
# --------------------------------------------------------------------------
async def sessions_to_run() -> list:
    """[(uid, string_session)] — sirf jinka Trial/Starter/Pro abhi active hai."""
    def fn(c):
        rows = c.execute(
            "SELECT telegram_user_id, session_enc FROM users WHERE session_enc IS NOT NULL "
            "AND sub_status IN ('trial','active') AND expires_at>?", (_now(),)).fetchall()
        return [(r["telegram_user_id"], r["session_enc"]) for r in rows]

    out = []
    for uid, enc in await _run(fn):
        s = decrypt_text(enc)
        if s:
            out.append((uid, s))
    return out


async def expired_subscriptions() -> list:
    """Jinka time khatam ho gaya par status abhi 'trial'/'active' hai (cleanup ko ye chahiye)."""
    return await _run(lambda c: [dict(r) for r in c.execute(
        "SELECT telegram_user_id, plan, billing, expires_at FROM users "
        "WHERE sub_status IN ('trial','active') AND expires_at<=?", (_now(),))])


async def mark_expired(uid: int) -> bool:
    """Status 'expired'. True sirf tab jab ab bhi expire hi hai (beech me renew hua to False)."""
    return await _run(lambda c: c.execute(
        "UPDATE users SET sub_status='expired' WHERE telegram_user_id=? AND sub_status IN ('trial','active') AND expires_at<=?",
        (uid, _now())).rowcount > 0)


async def mark_expiry_notified(uid: int) -> None:
    await _run(lambda c: c.execute("UPDATE users SET expiry_notified=1 WHERE telegram_user_id=?", (uid,)))


async def needs_expiry_notice() -> list:
    return await _run(lambda c: [dict(r) for r in c.execute(
        "SELECT telegram_user_id, plan FROM users WHERE sub_status='expired' AND expiry_notified=0")])
