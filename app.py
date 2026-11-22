"""
Profitix Ghost - FastAPI + Bot entrypoint.
Supports single-process mode (ENABLE_WORKER=true) and split Render+Kaggle mode (ENABLE_WORKER=false).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    missing = config.missing_config()
    if missing:
        log.warning("Missing environment variables: %s", ", ".join(missing))
    await _init_db_with_retry()

    tasks: List[asyncio.Task] = []
    if config.BOT_TOKEN:
        tasks.append(asyncio.create_task(_supervise("bot", bot_module.run_polling)))

    if config.ENABLE_WORKER:
        worker.set_notifier(bot_module.notify_expired)
        if config.API_ID and config.API_HASH:
            tasks.append(asyncio.create_task(_supervise("worker-restore", worker.restore_all)))
        tasks.append(asyncio.create_task(_supervise("cleanup", worker.cleanup_loop)))
        log.info("Embedded Telethon worker ENABLED")
    else:
        log.info("Split mode active (ENABLE_WORKER=false): Telethon worker delegated to external 30GB worker")

    tasks.append(asyncio.create_task(_supervise("otp-gc", telegram_manager.gc_loop)))
    log.info("Profitix Ghost web gateway is up on port %s", config.PORT)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if config.ENABLE_WORKER:
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
    return {
        "status": "ok",
        "mode": "embedded" if config.ENABLE_WORKER else "gateway",
        "active_workers": worker.active_count(),
    }


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ state
@app.get("/api/state")
async def api_state(user: dict = Depends(current_user)):
    uid = int(user["id"])
    await db.upsert_user(uid, user.get("first_name", ""), user.get("username", ""))
    status = await db.get_status(uid)
    settings = await db.get_settings(uid)
    account = await telegram_manager.get_account(uid)
    eligible, _ = await db.trial_eligibility(uid)
    active = status["status"] == "active"
    if active and status["plan"] in ("starter", "pro"):
        eligible = False
    tier = status["plan"] if active else None
    running = (worker.is_running(uid) if config.ENABLE_WORKER else bool(active and account)) and bool(settings["is_active"])
    return {
        "status_ok": True,
        "user": {
            "id": uid,
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
            "photo_url": user.get("photo_url", ""),
        },
        "status": status,
        "settings": settings,
        "account": account,
        "monitoring": running,
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
    fields = {}
    if body.is_active is not None:
        fields["is_active"] = body.is_active
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
    if config.ENABLE_WORKER:
        worker.invalidate_settings(uid)
        running = worker.is_running(uid) and bool(settings["is_active"])
    else:
        st = await db.get_status(uid)
        acc = await telegram_manager.get_account(uid)
        running = bool(st["status"] == "active" and acc and settings["is_active"])
    return {"status": "ok", "settings": settings, "monitoring": running}


# ------------------------------------------------------------------ Telegram connection
@app.post("/api/telegram/send-code")
async def api_send_code(body: PhoneIn, user: dict = Depends(current_user)):
    return await telegram_manager.send_code(int(user["id"]), body.phone)


@app.post("/api/telegram/verify-code")
async def api_verify_code(body: CodeIn, user: dict = Depends(current_user)):
    return await telegram_manager.verify_code(int(user["id"]), body.phone, body.code)


@app.post("/api/telegram/verify-2fa")
async def api_verify_2fa(body: TwoFAIn, user: dict = Depends(current_user)):
    return await telegram_manager.verify_2fa(int(user["id"]), body.phone, body.password)


@app.post("/api/telegram/disconnect")
async def api_disconnect(user: dict = Depends(current_user)):
    await telegram_manager.disconnect_account(int(user["id"]))
    return {"status": "ok"}


# ------------------------------------------------------------------ payments
_verify_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_last_verify: Dict[int, float] = {}


async def _guarded_verify(uid: int, run: Callable[[], Awaitable[payment.PaymentResult]]):
    lock = _verify_locks[uid]
    if lock.locked():
        raise ApiError(429, "A verification is already in progress.", "busy")
    async with lock:
        wait = 4 - (time.time() - _last_verify.get(uid, 0))
        if wait > 0:
            raise ApiError(429, f"Please wait {int(wait) + 1}s before trying again.", "rate_limited")
        _last_verify[uid] = time.time()
        result = await run()
    if not result.ok:
        return _err(400, result.message, result.code)
    if config.ENABLE_WORKER:
        await worker.ensure_running(uid)  # resume immediately in single-process mode
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
    return {"status": "ok", **payload}


@app.post("/api/payment/verify-upi")
async def api_verify_upi(body: UpiVerifyIn, user: dict = Depends(current_user)):
    uid = int(user["id"])
    await db.upsert_user(uid, user.get("first_name", ""), user.get("username", ""))
    return await _guarded_verify(uid, lambda: payment.verify_upi_payment(uid, body.plan_key, body.utr))


@app.post("/api/payment/verify-usdt")
async def api_verify_usdt(body: UsdtVerifyIn, user: dict = Depends(current_user)):
    uid = int(user["id"])
    await db.upsert_user(uid, user.get("first_name", ""), user.get("username", ""))
    return await _guarded_verify(uid, lambda: payment_usdt.verify_usdt_payment(uid, body.plan_key, body.tx_hash))


@app.get("/api/payments")
async def api_payments(user: dict = Depends(current_user)):
    return {"status": "ok", "payments": await db.list_payments(int(user["id"]), 20)}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="info")
