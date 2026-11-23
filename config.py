"""
Profitix Ghost - central configuration.
All secrets come from environment variables (Render / Kaggle Secrets).
"""
import logging
import os
from dataclasses import dataclass
from typing import Optional

try:  # local development convenience only
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = True) -> bool:
    val = _env(name, "")
    if not val:
        return default
    return val.lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)

# ---------------------------------------------------------------- core & split mode
BOT_TOKEN = _env("BOT_TOKEN")
API_ID = _env_int("API_ID", 0)
API_HASH = _env("API_HASH")
DATABASE_URL = _env("DATABASE_URL")
WEBAPP_URL = _env("WEBAPP_URL").rstrip("/")
PORT = _env_int("PORT", 7860)
ADMIN_ID = _env_int("ADMIN_ID", 0)
SUPPORT_USERNAME = _env("SUPPORT_USERNAME", "support").lstrip("@")

# Set ENABLE_WORKER=false on Render (512MB) when Kaggle (30GB) runs worker.py
ENABLE_WORKER = _env_bool("ENABLE_WORKER", True)

# ---------------------------------------------------------------- UPI / Gmail
UPI_ID = _env("UPI_ID")
UPI_PAYEE_NAME = _env("UPI_PAYEE_NAME", "Profitix Ghost")
GMAIL_USER = _env("GMAIL_USER")
GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD").replace(" ", "")
GMAIL_IMAP_HOST = "imap.gmail.com"
BANK_ALERT_SENDERS = [s.strip().lower() for s in _env("BANK_ALERT_SENDERS").split(",") if s.strip()]
UPI_EMAIL_LOOKBACK_DAYS = max(1, _env_int("UPI_EMAIL_LOOKBACK_DAYS", 3))

# ---------------------------------------------------------------- USDT BEP20
USDT_WALLET = _env("USDT_WALLET", "0x8f61C470a4fC19f79685BB60F8c6e3676BCf886B")
USDT_CONTRACT = _env("USDT_CONTRACT", "0x55d398326f99059fF775485246999027B3197955")
USDT_DECIMALS = 18  # Binance-Peg USDT on BSC uses 18 decimals
BSC_CHAIN_ID = 56
BSCSCAN_API_KEY = _env("BSCSCAN_API_KEY")
BSCSCAN_API_URL = _env("BSCSCAN_API_URL", "https://api.etherscan.io/v2/api")
USDT_MIN_CONFIRMATIONS = _env_int("USDT_MIN_CONFIRMATIONS", 3)
USDT_MAX_TX_AGE_HOURS = _env_int("USDT_MAX_TX_AGE_HOURS", 72)

# ---------------------------------------------------------------- runtime tuning
REPLY_CONCURRENCY = 10            # asyncio.Semaphore(10) for outgoing auto-replies
CLEANUP_INTERVAL_SECONDS = 600    # RAM cleanup scheduler: every 10 minutes
DB_SYNC_INTERVAL_SECONDS = 20     # Sync newly connected/paid users from Neon DB every 20s
# Smart "reply only when I'm offline" mode
OFFLINE_GRACE_SECONDS = 12            # wait so the owner can read / answer first
OWNER_ACTIVE_WINDOW_SECONDS = 240     # owner activity (sent / read) inside this window = online
OFFLINE_STATUS_CHECK = _env_bool("OFFLINE_STATUS_CHECK", True)  # also check Telegram "online" status
OTP_CACHE_TTL_SECONDS = 600       # pending Telegram login state lifetime
DEFAULT_COOLDOWN_SECONDS = 600    # reply once per sender every 10 minutes
REPLY_DELAY_MIN = 2
REPLY_DELAY_MAX = 5
ALLOWED_COOLDOWNS = (300, 900, 3600)  # 5m / 15m / 1h


# ---------------------------------------------------------------- plans
@dataclass(frozen=True)
class Plan:
    key: str
    tier: str              # trial | starter | pro
    period: str            # day | month | year
    name: str
    inr: int
    usdt: Optional[int]    # None -> UPI only
    days: int
    badge: str = ""


PLANS = {
    "trial": Plan("trial", "trial", "day", "1-Day Starter Trial", 2, None, 1),
    "starter_monthly": Plan("starter_monthly", "starter", "month", "Starter Monthly", 49, 1, 30),
    "starter_yearly": Plan("starter_yearly", "starter", "year", "Starter Yearly", 399, 8, 365, "SAVE 32%"),
    "pro_monthly": Plan("pro_monthly", "pro", "month", "Pro Monthly", 199, 3, 30),
    "pro_yearly": Plan("pro_yearly", "pro", "year", "Pro Yearly", 1499, 20, 365, "MOST POPULAR | SAVE 37%"),
}

TIER_FEATURES = {
    "trial": {"telegram": True, "whatsapp": False, "webhooks": False, "unlimited_rules": False},
    "starter": {"telegram": True, "whatsapp": False, "webhooks": False, "unlimited_rules": False},
    "pro": {"telegram": True, "whatsapp": True, "webhooks": True, "unlimited_rules": True},
}
MAX_RULES_LIMITED = 10  # keyword rule cap for non-pro tiers


def get_plan(key: str) -> Optional[Plan]:
    return PLANS.get(key)


def tier_has(tier: Optional[str], feature: str) -> bool:
    return bool(TIER_FEATURES.get(tier or "", {}).get(feature, False))


def public_plans() -> list:
    """Serializable plan catalog for the Mini App frontend."""
    return [
        {
            "key": p.key, "tier": p.tier, "period": p.period, "name": p.name,
            "inr": p.inr, "usdt": p.usdt, "days": p.days, "badge": p.badge,
        }
        for p in PLANS.values()
    ]


def missing_config() -> list:
    """Returns the names of required variables that are not set (logged at startup)."""
    required = {
        "BOT_TOKEN": BOT_TOKEN, "API_ID": API_ID, "API_HASH": API_HASH,
        "DATABASE_URL": DATABASE_URL, "WEBAPP_URL": WEBAPP_URL, "UPI_ID": UPI_ID,
        "GMAIL_USER": GMAIL_USER, "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
        "BSCSCAN_API_KEY": BSCSCAN_API_KEY,
    }
    return [k for k, v in required.items() if not v]
