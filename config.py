"""Profitix Ghost — configuration.

Saari secret values `.env` / environment se aati hain. Code me koi secret nahi hai.
"""
import os
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
#  .env loader (python-dotenv ki zaroorat nahi — Termux pe kam dependency)
# --------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        os.environ.setdefault(key, val)


_load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return _str(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list:
    return [x.strip() for x in _str(name, default).split(",") if x.strip()]


# --------------------------------------------------------------------------
#  Telegram / server
# --------------------------------------------------------------------------
APP_NAME = "Profitix Ghost"
BOT_TOKEN = _str("BOT_TOKEN")
BASE_URL = _str("BASE_URL").rstrip("/")
ADMIN_IDS = [int(x) for x in _list("ADMIN_IDS") if x.lstrip("-").isdigit()]
SUPPORT_URL = _str("SUPPORT_URL")

API_ID = _int("API_ID", 0)
API_HASH = _str("API_HASH")

HOST = _str("HOST", "0.0.0.0")
PORT = _int("PORT", 8000)

_db = _str("DB_PATH", "ghost.db")
DB_PATH = _db if os.path.isabs(_db) else str(BASE_DIR / _db)
SESSION_SECRET = _str("SESSION_SECRET")

# --------------------------------------------------------------------------
#  UPI + Gmail IMAP
# --------------------------------------------------------------------------
UPI_ID = _str("UPI_ID")
PAYEE_NAME = _str("PAYEE_NAME", APP_NAME)
IMAP_HOST = _str("IMAP_HOST", "imap.gmail.com")
IMAP_USER = _str("IMAP_USER")
IMAP_PASS = _str("IMAP_PASS")
IMAP_FOLDERS = _list("IMAP_FOLDERS", "INBOX")
ALLOWED_SENDER_DOMAINS = [d.lower().lstrip("@") for d in _list("ALLOWED_SENDER_DOMAINS")]
REQUIRE_DKIM = _bool("REQUIRE_DKIM", True)
UPI_MAIL_MAX_AGE_HOURS = _int("UPI_MAIL_MAX_AGE_HOURS", 72)

# --------------------------------------------------------------------------
#  USDT BEP20
# --------------------------------------------------------------------------
USDT_WALLET = _str("USDT_WALLET", "0x8f61C470a4fC19f79685BB60F8c6e3676BCf886B")
USDT_CONTRACT = _str("USDT_CONTRACT", "0x55d398326f99059fF775485246999027B3197955")
USDT_DECIMALS = 18  # BSC-USD (USDT BEP20) 18 decimals
BSCSCAN_API_KEY = _str("BSCSCAN_API_KEY")
BSCSCAN_API_URL = _str("BSCSCAN_API_URL", "https://api.etherscan.io/v2/api")
BSC_RPC_URLS = _list(
    "BSC_RPC_URLS",
    "https://bsc-dataseed.binance.org,https://bsc-dataseed1.defibit.io,https://rpc.ankr.com/bsc",
)
USDT_MIN_CONFIRMATIONS = _int("USDT_MIN_CONFIRMATIONS", 3)
USDT_MAX_AGE_HOURS = _int("USDT_MAX_AGE_HOURS", 48)
USDT_ALLOW_OVERPAY = _bool("USDT_ALLOW_OVERPAY", False)

# --------------------------------------------------------------------------
#  Worker
# --------------------------------------------------------------------------
REPLY_DELAY_MIN = _int("REPLY_DELAY_MIN", 2)
REPLY_DELAY_MAX = max(REPLY_DELAY_MIN, _int("REPLY_DELAY_MAX", 5))
REPLY_COOLDOWN_SEC = _int("REPLY_COOLDOWN_SEC", 60)
CLEANUP_INTERVAL_SEC = _int("CLEANUP_INTERVAL_SEC", 600)
OTP_TTL_SEC = _int("OTP_TTL_SEC", 600)

# --------------------------------------------------------------------------
#  Plans & pricing
# --------------------------------------------------------------------------
# tier -> kya-kya chalega
TIER_FEATURES = {
    "trial": {"telegram": True, "whatsapp": False, "webhooks": False},
    "starter": {"telegram": True, "whatsapp": False, "webhooks": False},
    "pro": {"telegram": True, "whatsapp": True, "webhooks": True},
}

# usdt = 0 -> USDT se nahi milta (trial sirf UPI se, sirf Telegram)
PLANS = {
    "trial": {
        "key": "trial", "tier": "trial", "name": "1-Day Starter Trial", "period": "1d",
        "inr": 2, "usdt": 0, "days": 1, "badge": "",
    },
    "starter_monthly": {
        "key": "starter_monthly", "tier": "starter", "name": "Starter", "period": "monthly",
        "inr": 49, "usdt": 1, "days": 30, "badge": "",
    },
    "starter_yearly": {
        "key": "starter_yearly", "tier": "starter", "name": "Starter", "period": "yearly",
        "inr": 399, "usdt": 8, "days": 365, "badge": "Save 32%",
    },
    "pro_monthly": {
        "key": "pro_monthly", "tier": "pro", "name": "Pro All-In-One", "period": "monthly",
        "inr": 199, "usdt": 3, "days": 30, "badge": "MOST POPULAR",
    },
    "pro_yearly": {
        "key": "pro_yearly", "tier": "pro", "name": "Pro All-In-One", "period": "yearly",
        "inr": 1499, "usdt": 20, "days": 365, "badge": "MOST POPULAR · Save 37%",
    },
}

for _p in PLANS.values():
    _p["features"] = TIER_FEATURES[_p["tier"]]


def plan_amount_inr(plan_key: str) -> Decimal:
    return Decimal(str(PLANS[plan_key]["inr"]))


def plan_amount_usdt(plan_key: str) -> Decimal:
    return Decimal(str(PLANS[plan_key]["usdt"]))


def public_plans() -> list:
    """Frontend ko bhejne layak plan list (koi secret nahi)."""
    return [dict(p) for p in PLANS.values()]


def validate(strict: bool = True) -> list:
    """Missing zaroori settings ki list. start.sh / app.py startup pe call karo."""
    need = {
        "BOT_TOKEN": BOT_TOKEN, "BASE_URL": BASE_URL, "API_ID": API_ID, "API_HASH": API_HASH,
        "SESSION_SECRET": SESSION_SECRET, "UPI_ID": UPI_ID, "IMAP_USER": IMAP_USER, "IMAP_PASS": IMAP_PASS,
    }
    missing = [k for k, v in need.items() if not v]
    if SESSION_SECRET and len(SESSION_SECRET) < 16:
        missing.append("SESSION_SECRET (kam se kam 16 chars)")
    if strict and not BSCSCAN_API_KEY and not BSC_RPC_URLS:
        missing.append("BSCSCAN_API_KEY ya BSC_RPC_URLS")
    return missing
