"""
Profitix Ghost - UPI payments.

* Pure-SVG QR codes (qrcode + SvgFillImage, no Pillow).
* UTR verification by reading bank credit alerts from Gmail via IMAP.
* Replay protection via the used_payments table.

RAZORPAY HOOK (future): to move to UPI AutoPay, add a new module (e.g. payment_razorpay.py)
that creates a mandate/subscription, verifies the webhook signature, and then calls
`finalize_payment(...)` below. Nothing else in the app needs to change because every payment
method funnels through that one function.
"""
import asyncio
import base64
import email
import hashlib
import imaplib
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import Message
from functools import lru_cache
from typing import Optional
from urllib.parse import quote

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from qrcode.image.svg import SvgFillImage

import config
import database as db

log = logging.getLogger("payment")

UTR_RE = re.compile(r"^\d{12}$")
AMOUNT_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
VPA_RE = re.compile(r"\b([A-Za-z0-9._\-]{2,64}@[A-Za-z]{2,32})(?![A-Za-z0-9.])")
TAG_RE = re.compile(r"<[^>]+>")
CREDIT_WORDS = ("credited", "credit", "received", "deposited")
MAX_SCAN_MESSAGES = 80


@dataclass
class PaymentResult:
    ok: bool
    code: str
    message: str
    plan_key: Optional[str] = None
    expires_at: Optional[str] = None


# ------------------------------------------------------------------ QR / links
def make_qr_svg(data: str) -> str:
    """Returns an inline-ready <svg> string (no XML prolog, no PIL needed)."""
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=2, image_factory=SvgFillImage)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf)
    svg = buf.getvalue().decode("utf-8")
    return re.sub(r"^<\?xml[^>]*\?>\s*", "", svg).strip()


@lru_cache(maxsize=64)
def make_qr_data_uri(data: str) -> str:
    svg = make_qr_svg(data)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def upi_deep_link(amount: int, note: str = "Profitix Ghost") -> str:
    return (
        f"upi://pay?pa={quote(config.UPI_ID, safe='@.')}"
        f"&pn={quote(config.UPI_PAYEE_NAME)}"
        f"&am={amount}.00&cu=INR&tn={quote(note)}"
    )


def build_upi_payload(plan_key: str) -> Optional[dict]:
    plan = config.get_plan(plan_key)
    if not plan:
        return None
    link = upi_deep_link(plan.inr, f"Profitix {plan.name}")
    return {
        "plan_key": plan.key,
        "plan_name": plan.name,
        "amount": plan.inr,
        "currency": "INR",
        "upi_id": config.UPI_ID,
        "deep_link": link,
        "qr_svg": make_qr_data_uri(link),
    }


# ------------------------------------------------------------------ Gmail helpers
def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _message_text(msg: Message) -> str:
    chunks = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype == "text/plain":
            chunks.append(_decode_part(part))
        elif ctype == "text/html":
            chunks.append(TAG_RE.sub(" ", _decode_part(part)))
    text = " ".join(chunks)
    return re.sub(r"\s+", " ", text)


def _sender_allowed(msg: Message) -> bool:
    if not config.BANK_ALERT_SENDERS:
        return True
    sender = (msg.get("From") or "").lower()
    return any(s in sender for s in config.BANK_ALERT_SENDERS)


def _amount_matches(text: str, expected: int) -> bool:
    target = Decimal(expected)
    for raw in AMOUNT_RE.findall(text):
        try:
            if Decimal(raw.replace(",", "")) == target:
                return True
        except InvalidOperation:
            continue
    return False


def _extract_payer(text: str) -> Optional[str]:
    own = config.UPI_ID.lower()
    for vpa in VPA_RE.findall(text):
        if vpa.lower() != own:
            return vpa.lower()
    return None


def hash_payer(payer: Optional[str]) -> Optional[str]:
    if not payer:
        return None
    return hashlib.sha256(payer.strip().lower().encode("utf-8")).hexdigest()


def _scan_message(raw: bytes, utr: str, amount: int) -> Optional[dict]:
    msg = email.message_from_bytes(raw)
    if not _sender_allowed(msg):
        return None
    text = _message_text(msg)
    lower = text.lower()
    if utr not in text:
        return None
    if not any(w in lower for w in CREDIT_WORDS):
        return None
    if not _amount_matches(text, amount):
        return None
    return {"payer": _extract_payer(text)}


def _imap_find_credit(utr: str, amount: int) -> Optional[dict]:
    """Blocking IMAP lookup. Always executed inside asyncio.to_thread."""
    since = (datetime.now(timezone.utc) - timedelta(days=config.UPI_EMAIL_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    conn = imaplib.IMAP4_SSL(config.GMAIL_IMAP_HOST, 993, timeout=25)
    try:
        conn.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
        conn.select("INBOX", readonly=True)

        candidate_ids = []
        typ, data = conn.search(None, "SINCE", since, "TEXT", f'"{utr}"')
        if typ == "OK" and data and data[0]:
            candidate_ids = data[0].split()

        if not candidate_ids:  # fallback: scan the most recent alerts in the window
            typ, data = conn.search(None, "SINCE", since)
            if typ == "OK" and data and data[0]:
                candidate_ids = data[0].split()[-MAX_SCAN_MESSAGES:]

        for msg_id in reversed(candidate_ids):
            typ, fetched = conn.fetch(msg_id, "(BODY.PEEK[])")
            if typ != "OK" or not fetched:
                continue
            for item in fetched:
                if isinstance(item, tuple) and len(item) > 1:
                    found = _scan_message(item[1], utr, amount)
                    if found:
                        return found
        return None
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ------------------------------------------------------------------ shared activation
async def finalize_payment(
    user_id: int, plan_key: str, method: str, ref_id: str,
    amount: float, currency: str, payer_hash: Optional[str] = None,
) -> PaymentResult:
    """
    Single funnel used by every payment method (UPI, USDT, future Razorpay):
    replay lock -> trial anti-abuse lock -> plan activation -> payment history.
    """
    plan = config.get_plan(plan_key)
    if not plan:
        return PaymentResult(False, "bad_plan", "Invalid plan selected.")

    if not await db.reserve_payment(ref_id, method, user_id, plan_key, amount):
        return PaymentResult(False, "already_used", "This payment reference was already used.")

    try:
        if plan.tier == "trial":
            if not await db.lock_trial(user_id, payer_hash):
                await db.add_payment(user_id, plan_key, amount, currency, method, "rejected", ref_id)
                return PaymentResult(
                    False, "trial_blocked",
                    "The Rs.2 trial can only be claimed once per user, payer UPI and Telegram account. "
                    "Please choose the Starter plan, or contact support for a refund.",
                )
        expires = await db.activate_plan(user_id, plan.tier, plan.period, plan.days)
        await db.add_payment(user_id, plan_key, amount, currency, method, "success", ref_id)
    except Exception:
        log.exception("Activation failed for user %s", user_id)
        await db.release_payment(ref_id)  # let the user retry with the same UTR
        return PaymentResult(False, "server_error", "Could not activate your plan. Please try again.")

    return PaymentResult(
        True, "ok", f"{plan.name} activated!", plan_key=plan.key, expires_at=expires.isoformat()
    )


# ------------------------------------------------------------------ public API
async def verify_upi_payment(user_id: int, plan_key: str, utr: str) -> PaymentResult:
    utr = (utr or "").strip()
    plan = config.get_plan(plan_key)
    if not plan:
        return PaymentResult(False, "bad_plan", "Invalid plan selected.")
    if not UTR_RE.match(utr):
        return PaymentResult(False, "bad_utr", "UTR must be exactly 12 digits.")
    if not (config.GMAIL_USER and config.GMAIL_APP_PASSWORD and config.UPI_ID):
        return PaymentResult(False, "not_configured", "UPI verification is not configured yet.")

    if plan.tier == "trial":
        eligible, _ = await db.trial_eligibility(user_id)
        if not eligible:
            return PaymentResult(False, "trial_blocked", "The Rs.2 trial has already been used.")

    if await db.is_payment_used(utr):
        return PaymentResult(False, "already_used", "This UTR was already used.")

    try:
        found = await asyncio.wait_for(
            asyncio.to_thread(_imap_find_credit, utr, plan.inr), timeout=45
        )
    except (imaplib.IMAP4.error, OSError, asyncio.TimeoutError):
        log.exception("Gmail IMAP check failed")
        return PaymentResult(False, "gmail_error", "Verification service is busy. Please retry in a minute.")

    if not found:
        return PaymentResult(
            False, "not_found",
            f"No credit of Rs.{plan.inr} with this UTR found yet. Bank alerts can take 1-2 minutes, "
            "please check the UTR and retry.",
        )

    return await finalize_payment(
        user_id, plan.key, "upi", utr, float(plan.inr), "INR", hash_payer(found.get("payer"))
    )
