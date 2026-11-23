"""
Profitix Ghost - web gateway (FastAPI + aiogram bot).

ENABLE_WORKER=false (Render):  FastAPI + Mini App + API + aiogram bot ONLY. The Telethon auto-reply
                               engine lives on the Kaggle worker (worker.py) and shares the Neon DB.
ENABLE_WORKER=true  (single server): the engine runs embedded. A DB lease still guarantees that only
                               one engine anywhere can reply, so duplicates are impossible.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qsl

import uvicorn
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

import bot as bot_module
import config
import database as db
import payment
import payment_usdt
import telegram_manager
import worker
from telegram_manager import TGError

log = logging.getLogger("app")
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_RULES_PRO = 50
TEMPLATES = {"busy", "hours", "sleep", "custom"}
ENABLE_WORKER = config.ENABLE_WORKER


# ------------------------------------------------------------------ errors
class ApiError(Exception):
    def __init__(self, status: int, error: str, code: str = ""):
        self.status, self.error, self.code = status, error, code


def _err(status: int, message: str, code: str = "", **extra) -> JSONResponse:
    return JSONResponse({"status": "error", "error": message, "code": code, **extra}, status_code=status)


# ------------------------------------------------------------------ background tasks
async def _supervise(name: str, factory: Callable[[], Awaitable], delay: float = 5.0) -> None:
    """Runs a long-lived task and restarts it with backoff if it crashes."""
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Task '%s' crashed, restarting in %ss", name, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def _init_db_with_retry(attempts: int = 6) -> None:
    for i in range(1, attempts + 1):
        try:
            await db.init_db()
            return
        except Exception:
            if i == attempts:
                raise
            log.warning("Database not ready (attempt %s/%s), retrying...", i, attempts)
            await asyncio.sleep(3 * i)  # Neon may be waking from idle


async def _prune_loop() -> None:
    while True:
        try:
            await db.prune_activity(30)
        except Exception:
            log.exception("Activity prune failed")
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    missing = config.missing_config()
    if missing:
        log.warning("Missing environment variables: %s", ", ".join(missing))
    await _init_db_with_retry()

    tasks: List[asyncio.Task] = []
    if config.BOT_TOKEN:
        tasks.append(asyncio.create_task(_supervise("bot", bot_module.run_polling)))

    if ENABLE_WORKER:
        if config.API_ID and config.API_HASH:
            tasks.append(asyncio.create_task(
                _supervise("worker-engine", lambda: worker.run_engine(bot_module.notify_expired))
            ))
            log.info("Embedded auto-reply engine ENABLED (lease-protected: only one engine can ever reply)")
        else:
            log.warning("ENABLE_WORKER=true but API_ID/API_HASH are missing: engine not started")
    else:
        log.info("Lightweight gateway mode (ENABLE_WORKER=false): auto-reply runs on the external worker")

    tasks.append(asyncio.create_task(_supervise("otp-gc", telegram_manager.gc_loop)))
    tasks.append(asyncio.create_task(_supervise("activity-prune", _prune_loop)))
    log.info("Profitix Ghost gateway is up on port %s", config.PORT)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if ENABLE_WORKER:
            await worker.shutdown()
        await telegram_manager.shutdown()
        await bot_module.close_bot()
        await db.close_db()


app = FastAPI(title="Profitix Ghost", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError):
    return _err(exc.status, exc.error, exc.code)


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(_: Request, exc: StarletteHTTPException):
    return _err(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _validation_handler(_: Request, exc: RequestValidationError):
    return _err(422, "Invalid input. Please check the fields and try again.", "validation")


@app.exception_handler(TGError)
async def _tg_error_handler(_: Request, exc: TGError):
    extra = {"retry_after": exc.retry_after} if exc.retry_after else {}
    return _err(exc.status, exc.message, exc.code, **extra)


# ------------------------------------------------------------------ Telegram WebApp auth
def verify_init_data(init_data: str) -> Optional[dict]:
    """Validates Telegram WebApp initData (HMAC-SHA256 with the bot token)."""
    if not init_data or not config.BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user if isinstance(user, dict) and "id" in user else None
    except (ValueError, TypeError):
        return None


async def current_user(x_telegram_init_data: str = Header(default="")) -> dict:
    user = verify_init_data(x_telegram_init_data)
    if not user:
        raise ApiError(401, "Please open this app from Telegram.", "unauthorized")
    return user


async def admin_user(user: dict = Depends(current_user)) -> dict:
    if not config.ADMIN_ID or int(user["id"]) != config.ADMIN_ID:
        raise ApiError(403, "Admins only.", "forbidden")
    return user


def _is_admin(user: dict) -> bool:
    return bool(config.ADMIN_ID) and int(user["id"]) == config.ADMIN_ID


async def track(user_id: int, kind: str, **meta) -> None:
    """Writes one line to the admin live feed. Never raises."""
    await db.log_event(user_id, kind, meta)


async def _monitoring(uid: int, settings: dict, status: Optional[dict] = None, has_account: Optional[bool] = None) -> bool:
    """True when this user's auto-reply is genuinely live right now."""
    if not settings.get("is_active"):
        return False
    if ENABLE_WORKER and worker.is_running(uid):
        return True
    if status is None:
        status = await db.get_status(uid)
    if has_account is None:
        has_account = (await db.get_tg_session(uid)) is not None
    if status["status"] != "active" or not has_account:
        return False
    online, _ = await worker.engine_status()
    return online


# ------------------------------------------------------------------ request models
class PhoneIn(BaseModel):
    phone: str = Field(min_length=5, max_length=25)


class CodeIn(PhoneIn):
    code: str = Field(min_length=4, max_length=12)


class TwoFAIn(PhoneIn):
    password: str = Field(min_length=1, max_length=256)


class Rule(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    reply: str = Field(min_length=1, max_length=1000)


class SettingsIn(BaseModel):
    is_active: Optional[bool] = None
    only_when_offline: Optional[bool] = None
    template: Optional[str] = Field(default=None, max_length=20)
    default_reply: Optional[str] = Field(default=None, max_length=1000)
    keyword_rules: Optional[List[Rule]] = Field(default=None, max_length=MAX_RULES_PRO)
    cooldown_seconds: Optional[int] = None


class UpiVerifyIn(BaseModel):
    plan_key: str = Field(max_length=30)
    utr: str = Field(min_length=1, max_length=40)


class UsdtVerifyIn(BaseModel):
    plan_key: str = Field(max_length=30)
    tx_hash: str = Field(min_length=1, max_length=100)


# ------------------------------------------------------------------ public routes
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    if ENABLE_WORKER:
        online, count = worker.is_engine_active(), worker.active_count()
        if not online:
            online, count = await worker.engine_status()
    else:
        online, count = await worker.engine_status()
    return {
        "status": "ok",
        "mode": "embedded" if ENABLE_WORKER else "gateway",
        "worker_online": online,
        "active_workers": count,
    }


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/ping")
async def api_ping(user: dict = Depends(current_user)):
    await db.touch_user(int(user["id"]))
    return {"status": "ok"}


# ------------------------------------------------------------------ state
@app.get("/api/state")
async def api_state(opened: int = Query(0, alias="open"), user: dict = Depends(current_user)):
    uid = int(user["id"])
    await db.upsert_user(uid, user.get("first_name", ""), user.get("username", ""))
    await db.touch_user(uid)
    if opened:
        await track(uid, "app_open")
    status = await db.get_status(uid)
    settings = await db.get_settings(uid)
    account = await telegram_manager.get_account(uid)
    eligible, _ = await db.trial_eligibility(uid)
    active = status["status"] == "active"
    if active and status["plan"] in ("starter", "pro"):
        eligible = False
    tier = status["plan"] if active else None
    return {
        "status_ok": True,
        "is_admin": _is_admin(user),
        "user": {
            "id": uid,
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
            "photo_url": user.get("photo_url", ""),
        },
        "status": status,
        "settings": settings,
        "account": account,
        "monitoring": await _monitoring(uid, settings, status, account is not None),
        "plans": config.public_plans(),
        "features": {f: config.tier_has(tier, f) for f in ("telegram", "whatsapp", "webhooks")},
        "max_rules": MAX_RULES_PRO if config.tier_has(tier, "unlimited_rules") else config.MAX_RULES_LIMITED,
        "trial_eligible": eligible,
        "support": config.SUPPORT_USERNAME,
        "allowed_cooldowns": list(config.ALLOWED_COOLDOWNS),
    }


# ------------------------------------------------------------------ settings
@app.post("/api/settings")
async def api_settings(body: SettingsIn, user: dict = Depends(current_user)):
    uid = int(user["id"])
    fields: dict = {}
    if body.is_active is not None:
        fields["is_active"] = body.is_active
    if body.only_when_offline is not None:
        fields["only_when_offline"] = body.only_when_offline
    if body.template is not None:
        if body.template not in TEMPLATES:
            raise ApiError(422, "Unknown template.")
        fields["template"] = body.template
    if body.default_reply is not None:
        reply = body.default_reply.strip()
        if not reply:
            raise ApiError(422, "Default reply cannot be empty.")
        fields["default_reply"] = reply
    if body.cooldown_seconds is not None:
        if body.cooldown_seconds not in config.ALLOWED_COOLDOWNS:
            raise ApiError(422, "Cooldown must be 5 minutes, 15 minutes or 1 hour.")
        fields["cooldown_seconds"] = body.cooldown_seconds
    if body.keyword_rules is not None:
        status = await db.get_status(uid)
        tier = status["plan"] if status["status"] == "active" else None
        limit = MAX_RULES_PRO if config.tier_has(tier, "unlimited_rules") else config.MAX_RULES_LIMITED
        if len(body.keyword_rules) > limit:
            raise ApiError(403, f"Your plan allows up to {limit} keyword rules. Upgrade to Pro for unlimited.", "rules_limit")
        fields["keyword_rules"] = [
            {"keyword": r.keyword.strip(), "reply": r.reply.strip()}
            for r in body.keyword_rules if r.keyword.strip() and r.reply.strip()
        ]
    settings = await db.save_settings(uid, **fields)
    worker.invalidate_settings(uid)
    if body.is_active is not None and len(fields) == 1:
        await track(uid, "reply_toggled", on=bool(body.is_active))
    else:
        await track(uid, "settings_saved", smart=bool(settings.get("only_when_offline", True)))
    return {"status": "ok", "settings": settings, "monitoring": await _monitoring(uid, settings)}


# ------------------------------------------------------------------ Telegram connection
@app.post("/api/telegram/send-code")
async def api_send_code(body: PhoneIn, user: dict = Depends(current_user)):
    uid = int(user["id"])
    try:
        result = await telegram_manager.send_code(uid, body.phone)
    except TGError as exc:
        await track(uid, "tg_connect_failed", step="send_code", code=exc.code)
        raise
    await track(uid, "tg_code_sent")
    return result


@app.post("/api/telegram/verify-code")
async def api_verify_code(body: CodeIn, user: dict = Depends(current_user)):
    uid = int(user["id"])
    try:
        result = await telegram_manager.verify_code(uid, body.phone, body.code)
    except TGError as exc:
        await track(uid, "tg_connect_failed", step="code", code=exc.code)
        raise
    if result.get("status") == "connected":
        await track(uid, "tg_connected")
    return result


@app.post("/api/telegram/verify-2fa")
async def api_verify_2fa(body: TwoFAIn, user: dict = Depends(current_user)):
    uid = int(user["id"])
    try:
        result = await telegram_manager.verify_2fa(uid, body.phone, body.password)
    except TGError as exc:
        await track(uid, "tg_connect_failed", step="2fa", code=exc.code)
        raise
    if result.get("status") == "connected":
        await track(uid, "tg_connected")
    return result


@app.post("/api/telegram/disconnect")
async def api_disconnect(user: dict = Depends(current_user)):
    uid = int(user["id"])
    await telegram_manager.disconnect_account(uid)
    await track(uid, "tg_disconnected")
    return {"status": "ok"}


# ------------------------------------------------------------------ payments
_verify_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_last_verify: Dict[int, float] = {}


async def _guarded_verify(uid: int, plan_key: str, method: str, run: Callable[[], Awaitable[payment.PaymentResult]]):
    plan = config.get_plan(plan_key)
    lock = _verify_locks[uid]
    if lock.locked():
        raise ApiError(429, "A verification is already in progress.", "busy")
    async with lock:
        wait = 4 - (time.time() - _last_verify.get(uid, 0))
        if wait > 0:
            raise ApiError(429, f"Please wait {int(wait) + 1}s before trying again.", "rate_limited")
        _last_verify[uid] = time.time()
        await track(uid, "pay_attempt", plan=plan_key, method=method)
        result = await run()
    amount = (plan.usdt if method == "usdt" else plan.inr) if plan else 0
    currency = "USDT" if method == "usdt" else "INR"
    if not result.ok:
        await track(uid, "pay_failed", plan=plan_key, method=method, code=result.code, amount=amount, currency=currency)
        return _err(400, result.message, result.code)
    await track(uid, "pay_success", plan=plan_key, method=method, amount=amount, currency=currency)
    await worker.ensure_running(uid)  # instant resume when this process is the engine; otherwise next sync (<=20s)
    status = await db.get_status(uid)
    return {"status": "ok", "message": result.message, "plan_key": result.plan_key,
            "expires_at": result.expires_at, "plan_status": status}


@app.get("/api/payment/info")
async def api_payment_info(
    plan_key: str = Query(max_length=30),
    method: str = Query(default="upi", pattern="^(upi|usdt)$"),
    user: dict = Depends(current_user),
):
    payload = payment.build_upi_payload(plan_key) if method == "upi" else payment_usdt.build_usdt_payload(plan_key)
    if not payload:
        raise ApiError(404, "This payment option is not available for the selected plan.")
    await track(int(user["id"]), "qr_generated", plan=plan_key, method=method, amount=payload["amount"])
    return {"status": "ok", **payload}


@app.post("/api/payment/verify-upi")
async def api_verify_upi(body: UpiVerifyIn, user: dict = Depends(current_user)):
    uid = int(user["id"])
    await db.upsert_user(uid, user.get("first_name", ""), user.get("username", ""))
    return await _guarded_verify(uid, body.plan_key, "upi", lambda: payment.verify_upi_payment(uid, body.plan_key, body.utr))


@app.post("/api/payment/verify-usdt")
async def api_verify_usdt(body: UsdtVerifyIn, user: dict = Depends(current_user)):
    uid = int(user["id"])
    await db.upsert_user(uid, user.get("first_name", ""), user.get("username", ""))
    return await _guarded_verify(uid, body.plan_key, "usdt", lambda: payment_usdt.verify_usdt_payment(uid, body.plan_key, body.tx_hash))


@app.get("/api/payments")
async def api_payments(user: dict = Depends(current_user)):
    return {"status": "ok", "payments": await db.list_payments(int(user["id"]), 20)}


# ------------------------------------------------------------------ admin panel (ADMIN_ID only)
@app.get("/api/admin/overview")
async def api_admin_overview(_: dict = Depends(admin_user)):
    data = await db.admin_overview()
    online, count = await worker.engine_status()
    if ENABLE_WORKER and worker.is_engine_active():
        online, count = True, worker.active_count()
    data["worker"] = {
        "online": online, "clients": count,
        "mode": "embedded" if ENABLE_WORKER else "gateway",
    }
    data["server_time"] = datetime.now(timezone.utc).isoformat()
    return {"status": "ok", **data}


@app.get("/api/admin/feed")
async def api_admin_feed(
    after: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=100), _: dict = Depends(admin_user),
):
    return {"status": "ok", "events": await db.admin_feed(after, limit)}


@app.get("/api/admin/users")
async def api_admin_users(
    q: str = Query("", max_length=60), offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
    _: dict = Depends(admin_user),
):
    return {"status": "ok", **(await db.admin_users(q, limit, offset))}


@app.get("/api/admin/payments")
async def api_admin_payments(_: dict = Depends(admin_user)):
    return {"status": "ok", "payments": await db.admin_payments(40)}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="info")
