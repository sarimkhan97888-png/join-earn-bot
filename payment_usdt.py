"""
Profitix Ghost - USDT (BEP20 / BSC Mainnet) payments.

Verification reads the transaction receipt through the BscScan / Etherscan-V2 "proxy" API,
decodes the ERC-20 Transfer event of the USDT contract and checks:
  * tx succeeded and has enough confirmations
  * token contract == USDT BEP20 contract
  * recipient == our receiving wallet
  * transferred amount == exact plan amount
  * tx is recent (anti old-hash abuse)
  * tx_hash was never redeemed before (replay lock in used_payments)
"""
import logging
import re
import time
from decimal import Decimal
from typing import Optional

import httpx

import config
import database as db
from payment import PaymentResult, finalize_payment, make_qr_data_uri

log = logging.getLogger("payment_usdt")

TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


# ------------------------------------------------------------------ payload for the UI
def build_usdt_payload(plan_key: str) -> Optional[dict]:
    plan = config.get_plan(plan_key)
    if not plan or plan.usdt is None:
        return None
    return {
        "plan_key": plan.key,
        "plan_name": plan.name,
        "amount": plan.usdt,
        "currency": "USDT",
        "network": "BEP20 (BNB Smart Chain)",
        "wallet": config.USDT_WALLET,
        "contract": config.USDT_CONTRACT,
        # Plain address QR: scannable by every wallet app
        "qr_svg": make_qr_data_uri(config.USDT_WALLET),
    }


# ------------------------------------------------------------------ BscScan RPC proxy
async def _rpc(client: httpx.AsyncClient, action: str, **params) -> object:
    query = {
        "chainid": config.BSC_CHAIN_ID,
        "module": "proxy",
        "action": action,
        "apikey": config.BSCSCAN_API_KEY,
        **params,
    }
    resp = await client.get(config.BSCSCAN_API_URL, params=query)
    resp.raise_for_status()
    body = resp.json()
    if "result" not in body:
        raise RuntimeError(f"Unexpected API response: {str(body)[:200]}")
    result = body["result"]
    # Rate-limit / key errors come back as {"status":"0","result":"<message string>"}
    if isinstance(result, str) and body.get("status") == "0":
        raise RuntimeError(result)
    return result


def _hex_to_int(value: str) -> int:
    return int(value, 16)


def _received_amounts(receipt: dict) -> list:
    """All USDT amounts sent to our wallet inside this transaction."""
    wallet = config.USDT_WALLET.lower()
    contract = config.USDT_CONTRACT.lower()
    amounts = []
    for entry in receipt.get("logs", []):
        topics = entry.get("topics") or []
        if (
            (entry.get("address") or "").lower() == contract
            and len(topics) >= 3
            and topics[0].lower() == TRANSFER_TOPIC
            and ("0x" + topics[2][-40:]).lower() == wallet
        ):
            raw = _hex_to_int(entry.get("data") or "0x0")
            amounts.append(Decimal(raw) / Decimal(10 ** config.USDT_DECIMALS))
    return amounts


# ------------------------------------------------------------------ public API
async def verify_usdt_payment(user_id: int, plan_key: str, tx_hash: str) -> PaymentResult:
    tx_hash = (tx_hash or "").strip()
    plan = config.get_plan(plan_key)
    if not plan or plan.usdt is None:
        return PaymentResult(False, "bad_plan", "USDT is not available for this plan (UPI only).")
    if not TX_HASH_RE.match(tx_hash):
        return PaymentResult(False, "bad_hash", "Invalid transaction hash. It must start with 0x and be 66 characters.")
    if not config.BSCSCAN_API_KEY:
        return PaymentResult(False, "not_configured", "USDT verification is not configured yet.")

    if await db.is_payment_used(tx_hash):
        return PaymentResult(False, "already_used", "This transaction hash was already used.")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            receipt = await _rpc(client, "eth_getTransactionReceipt", txhash=tx_hash)
            if not isinstance(receipt, dict):
                return PaymentResult(
                    False, "not_found",
                    "Transaction not found yet. Wait a minute for BSC confirmation and retry.",
                )
            if receipt.get("status") != "0x1":
                return PaymentResult(False, "tx_failed", "This transaction failed on-chain.")

            block_hex = receipt.get("blockNumber")
            latest_hex = await _rpc(client, "eth_blockNumber")
            confirmations = _hex_to_int(latest_hex) - _hex_to_int(block_hex)
            if confirmations < config.USDT_MIN_CONFIRMATIONS:
                return PaymentResult(
                    False, "pending",
                    f"Waiting for confirmations ({max(confirmations, 0)}/{config.USDT_MIN_CONFIRMATIONS}). Retry in a moment.",
                )

            block = await _rpc(client, "eth_getBlockByNumber", tag=block_hex, boolean="false")
            if isinstance(block, dict) and block.get("timestamp"):
                age_hours = (time.time() - _hex_to_int(block["timestamp"])) / 3600
                if age_hours > config.USDT_MAX_TX_AGE_HOURS:
                    return PaymentResult(
                        False, "too_old",
                        f"This transaction is older than {config.USDT_MAX_TX_AGE_HOURS} hours. Contact support.",
                    )
    except (httpx.HTTPError, RuntimeError, ValueError):
        log.exception("BscScan verification failed")
        return PaymentResult(False, "api_error", "Blockchain explorer is busy. Please retry in a minute.")

    amounts = _received_amounts(receipt)
    if not amounts:
        return PaymentResult(
            False, "wrong_recipient",
            "No USDT (BEP20) transfer to our wallet was found in this transaction.",
        )
    if Decimal(plan.usdt) not in amounts:
        sent = ", ".join(f"{a.normalize():f}" for a in amounts)
        return PaymentResult(
            False, "wrong_amount",
            f"Amount mismatch. Expected exactly {plan.usdt} USDT but received {sent} USDT.",
        )

    return await finalize_payment(
        user_id, plan.key, "usdt", tx_hash, float(plan.usdt), "USDT"
    )
