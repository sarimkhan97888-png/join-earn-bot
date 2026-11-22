"""Profitix Ghost — USDT (BEP20 / BSC) payments.

* Receiving wallet ka SVG QR + copyable address + exact USDT amount
* Transaction hash verify: BscScan API (BSC mainnet) -> fail ho to public BSC RPC
  - receipt status success, USDT contract ka Transfer event, recipient == apna wallet
  - exact token amount (USDT_ALLOW_OVERPAY=1 ho to zyada bhi chalega, kam kabhi nahi)
  - kam se kam N confirmations, aur tx bahut purani na ho
* Duplicate tx_hash reuse -> database.used_tx (PRIMARY KEY) se block
"""
import asyncio
import logging
import re
import time
from decimal import Decimal

import aiohttp

import config
import database as db
from payment import make_qr_svg, svg_data_uri

log = logging.getLogger("ghost.usdt")

TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)

_last_try: dict = {}
TRY_GAP_SEC = 8


# --------------------------------------------------------------------------
#  QR / payment info
# --------------------------------------------------------------------------
def usdt_payment_info(plan_key: str) -> dict:
    p = config.PLANS[plan_key]
    if p["usdt"] <= 0:
        raise ValueError("This plan is not available with USDT")
    svg = make_qr_svg(config.USDT_WALLET)
    return {
        "plan_key": plan_key, "name": p["name"], "period": p["period"],
        "network": "BEP20 (BNB Smart Chain)", "token": "USDT",
        "address": config.USDT_WALLET, "contract": config.USDT_CONTRACT,
        "amount": p["usdt"], "qr_svg": svg, "qr_data_uri": svg_data_uri(svg),
    }


def expected_wei(plan_key: str) -> int:
    return int(config.plan_amount_usdt(plan_key) * (Decimal(10) ** config.USDT_DECIMALS))


# --------------------------------------------------------------------------
#  Chain access: BscScan proxy API -> public RPC fallback
# --------------------------------------------------------------------------
async def _bscscan(session, action: str, extra: dict):
    if not config.BSCSCAN_API_KEY:
        return None
    params = {"module": "proxy", "action": action, "apikey": config.BSCSCAN_API_KEY, **extra}
    if "/v2/" in config.BSCSCAN_API_URL:
        params["chainid"] = "56"
    async with session.get(config.BSCSCAN_API_URL, params=params) as r:
        data = await r.json(content_type=None)
    res = data.get("result") if isinstance(data, dict) else None
    if isinstance(res, dict) or (isinstance(res, str) and res.startswith("0x")):
        return res
    return None


async def _rpc(session, method: str, params: list):
    for url in config.BSC_RPC_URLS:
        try:
            async with session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as r:
                data = await r.json(content_type=None)
            if isinstance(data, dict) and data.get("result") is not None:
                return data["result"]
        except Exception as e:
            log.debug("rpc %s failed: %s", url, e)
    return None


async def _call(session, method: str, rpc_params: list, api_extra: dict):
    try:
        res = await _bscscan(session, method, api_extra)
        if res is not None:
            return res
    except Exception as e:
        log.warning("BscScan %s failed: %s", method, e)
    return await _rpc(session, method, rpc_params)


async def _chain_data(tx_hash: str):
    """(receipt, latest_block_number, block_timestamp) ya (None, None, None)."""
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, trust_env=True) as s:
        receipt = await _call(s, "eth_getTransactionReceipt", [tx_hash], {"txhash": tx_hash})
        if not isinstance(receipt, dict):
            return None, None, None
        latest = await _call(s, "eth_blockNumber", [], {})
        blk = await _call(s, "eth_getBlockByNumber", [receipt.get("blockNumber"), False],
                          {"tag": receipt.get("blockNumber"), "boolean": "false"})
        ts = int(blk["timestamp"], 16) if isinstance(blk, dict) and blk.get("timestamp") else None
        return receipt, (int(latest, 16) if isinstance(latest, str) else None), ts


# --------------------------------------------------------------------------
#  Verification logic (pure — test karna aasan)
# --------------------------------------------------------------------------
def check_receipt(receipt: dict, latest_block, block_ts, plan_key: str, now: float = None) -> tuple:
    """(True, paid_amount_decimal) ya (False, reason)."""
    now = now or time.time()
    if receipt.get("status") != "0x1":
        return False, "failed_tx"
    wallet = config.USDT_WALLET.lower()
    contract = config.USDT_CONTRACT.lower()
    want = expected_wei(plan_key)
    best = 0
    for lg in receipt.get("logs", []) or []:
        topics = [t.lower() for t in (lg.get("topics") or [])]
        if (lg.get("address", "").lower() != contract or len(topics) < 3 or topics[0] != TRANSFER_TOPIC):
            continue
        if "0x" + topics[2][-40:] != wallet:
            continue
        try:
            best = max(best, int(lg.get("data", "0x0"), 16))
        except ValueError:
            continue
    if best == 0:
        return False, "no_transfer"
    ok_amount = best >= want if config.USDT_ALLOW_OVERPAY else best == want
    if not ok_amount:
        return False, "amount_mismatch"
    try:
        conf = (latest_block - int(receipt["blockNumber"], 16)) if latest_block is not None else 0
    except (KeyError, ValueError, TypeError):
        conf = 0
    if conf < config.USDT_MIN_CONFIRMATIONS:
        return False, "not_confirmed"
    if block_ts is None or now - block_ts > config.USDT_MAX_AGE_HOURS * 3600:
        return False, "too_old"
    return True, Decimal(best) / (Decimal(10) ** config.USDT_DECIMALS)


MESSAGES = {
    "bad_plan": "This plan is not available with USDT.",
    "bad_hash": "Invalid transaction hash. It should start with 0x and be 66 characters long.",
    "tx_used": "This transaction hash has already been used.",
    "duplicate_ref": "This transaction hash has already been used.",
    "too_fast": "Please wait a few seconds before trying again.",
    "not_found": "Transaction not found yet. Wait a minute for it to confirm and try again.",
    "failed_tx": "This transaction failed on-chain.",
    "no_transfer": "No USDT (BEP20) transfer to our wallet found in this transaction.",
    "amount_mismatch": "The USDT amount does not match this plan's price.",
    "not_confirmed": "Transaction is not confirmed yet. Try again in a minute.",
    "too_old": "This transaction is too old to be accepted. Contact support.",
    "chain_error": "Could not reach the blockchain right now. Please try again in a minute.",
    "no_user": "Please open the bot with /start first.",
    "trial_upi_only": "The ₹2 trial is available only via UPI.",
}


def _err(reason: str) -> dict:
    return {"ok": False, "reason": reason, "message": MESSAGES.get(reason, "Verification failed. Contact support.")}


async def verify_and_activate_usdt(user_id: int, plan_key: str, tx_hash: str) -> dict:
    plan = config.PLANS.get(plan_key)
    if not plan or plan["usdt"] <= 0:
        return _err("bad_plan")
    tx_hash = (tx_hash or "").strip()
    if not TX_RE.match(tx_hash):
        return _err("bad_hash")
    tx_hash = tx_hash.lower()
    now = time.monotonic()
    if now - _last_try.get(user_id, 0) < TRY_GAP_SEC:
        return _err("too_fast")
    _last_try[user_id] = now

    if await db.tx_used(tx_hash):
        return _err("tx_used")
    if not await db.get_user(user_id):
        return _err("no_user")

    try:
        receipt, latest, ts = await _chain_data(tx_hash)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("chain error: %s", e)
        return _err("chain_error")
    if receipt is None:
        return _err("not_found")

    ok, info = check_receipt(receipt, latest, ts, plan_key)
    if not ok:
        return _err(info)

    res = await db.activate_tx_payment(user_id, plan_key, tx_hash, float(info))
    if not res["ok"]:
        return _err(res["reason"])
    res["message"] = f"{plan['name']} activated!"
    return res
