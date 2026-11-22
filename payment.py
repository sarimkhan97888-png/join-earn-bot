"""Profitix Ghost — UPI payments.

* upi://pay QR  -> pure SVG (qrcode + SvgFillImage, Pillow ki zaroorat nahi, Termux friendly)
* 12-digit UTR verify -> Gmail IMAP me bank/UPI credit-alert mail dhoondh kar (exact amount + UTR)
* Duplicate UTR reuse -> database.used_utrs (PRIMARY KEY) se block
"""
import asyncio
import base64
import email
import hashlib
import imaplib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from urllib.parse import quote

import qrcode
import qrcode.constants
import qrcode.image.svg

import config
import database as db

log = logging.getLogger("ghost.payment")

UTR_RE = re.compile(r"^\d{12}$")
AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
VPA_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9._\-]{1,62})@([A-Za-z][A-Za-z0-9]{1,30})\b(?!\.)")
CREDIT_WORDS = ("credited", "credit alert", "received", "deposited", "paid you", "sent you", "added to")

_last_try: dict = {}   # user_id -> time (IMAP ko hammer hone se bachane ke liye)
TRY_GAP_SEC = 8


# --------------------------------------------------------------------------
#  QR (SVG)
# --------------------------------------------------------------------------
def make_qr_svg(data: str) -> str:
    """Text -> inline <svg> string. Responsive (width/height hata ke viewBox rakha)."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgFillImage)
    raw = img.to_string()
    svg = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return _responsive_svg(svg)


def _responsive_svg(svg: str) -> str:
    svg = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", svg)
    m = re.search(r"<svg\b[^>]*>", svg)
    if not m:
        return svg
    tag = m.group(0)
    new = tag
    if "viewBox" not in new:
        w = re.search(r'\bwidth="([0-9.]+)', new)
        h = re.search(r'\bheight="([0-9.]+)', new)
        if w and h:
            new = new[:-1] + f' viewBox="0 0 {w.group(1)} {h.group(1)}">'
    new = re.sub(r'\s(?:width|height)="[^"]*"', "", new)
    new = new[:-1] + ' style="width:100%;height:auto;max-width:100%" shape-rendering="crispEdges">'
    return svg.replace(tag, new, 1)


def svg_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode()


def _fmt_amount(amount) -> str:
    return f"{Decimal(str(amount)):.2f}"


def upi_uri(amount) -> str:
    return f"upi://pay?pa={quote(config.UPI_ID, safe='@.-_')}&pn={quote(config.PAYEE_NAME)}&am={_fmt_amount(amount)}&cu=INR"


def upi_payment_info(plan_key: str) -> dict:
    """Payment modal ke liye: QR (inline svg + data URI), UPI ID, exact amount."""
    p = config.PLANS[plan_key]
    uri = upi_uri(p["inr"])
    svg = make_qr_svg(uri)
    return {
        "plan_key": plan_key, "name": p["name"], "period": p["period"],
        "amount": p["inr"], "upi_id": config.UPI_ID, "payee": config.PAYEE_NAME,
        "upi_uri": uri, "qr_svg": svg, "qr_data_uri": svg_data_uri(svg),
    }


# --------------------------------------------------------------------------
#  Email parsing
# --------------------------------------------------------------------------
def _dec(v) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _body_text(msg: Message) -> str:
    chunks = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            txt = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if part.get_content_subtype() == "html":
            txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", txt)
            txt = unescape(re.sub(r"<[^>]+>", " ", txt))
        chunks.append(txt)
    return re.sub(r"\s+", " ", " ".join(chunks))


def _amounts(text: str) -> set:
    out = set()
    for m in AMOUNT_RE.finditer(text):
        try:
            out.add(Decimal(m.group(1).replace(",", "")))
        except InvalidOperation:
            pass
    return out


def _payer_id(text: str):
    """Mail se payer ka UPI ID (VPA) nikaalo (apna UPI_ID chhod ke). None = nahi mila."""
    own = config.UPI_ID.lower()
    for m in VPA_RE.finditer(text):
        vpa = f"{m.group(1)}@{m.group(2)}".lower()
        if vpa != own:
            return vpa
    return None


def hash_payer(vpa: str):
    return hashlib.sha256(("ghost-payer|" + vpa.lower()).encode()).hexdigest() if vpa else None


def match_message(msg: Message, utr: str, amount: Decimal, now: datetime = None):
    """Ek mail check karo. Match hua to {'payer_hash':..} warna (None, reason)."""
    now = now or datetime.now(timezone.utc)
    sender = parseaddr(_dec(msg.get("From")))[1].lower()
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    allowed = config.ALLOWED_SENDER_DOMAINS
    if allowed and not any(domain == d or domain.endswith("." + d) for d in allowed):
        return None, "sender_not_allowed"
    if config.REQUIRE_DKIM:
        auth = " ".join(msg.get_all("Authentication-Results", []) or []).lower()
        if "dkim=pass" not in auth:
            return None, "dkim_fail"
    try:
        sent = parsedate_to_datetime(msg.get("Date"))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        if now - sent > timedelta(hours=config.UPI_MAIL_MAX_AGE_HOURS):
            return None, "too_old"
    except Exception:
        return None, "bad_date"
    text = _body_text(msg) + " " + _dec(msg.get("Subject"))
    if not re.search(rf"(?<!\d){re.escape(utr)}(?!\d)", text):
        return None, "utr_missing"
    low = text.lower()
    if not any(w in low for w in CREDIT_WORDS) or ("debited" in low and "credited" not in low):
        return None, "not_credit"
    if amount not in _amounts(text):
        return None, "amount_mismatch"
    return {"payer_hash": hash_payer(_payer_id(text))}, "ok"


# --------------------------------------------------------------------------
#  IMAP scan (blocking -> thread me chalta hai)
# --------------------------------------------------------------------------
def _imap_scan(utr: str, amount: Decimal) -> dict:
    if not (config.IMAP_USER and config.IMAP_PASS):
        return {"found": False, "reason": "imap_not_configured"}
    since = (datetime.now(timezone.utc) - timedelta(hours=config.UPI_MAIL_MAX_AGE_HOURS + 24)).strftime("%d-%b-%Y")
    last_reason = "not_found"
    M = imaplib.IMAP4_SSL(config.IMAP_HOST, timeout=30)
    try:
        M.login(config.IMAP_USER, config.IMAP_PASS)
        for folder in config.IMAP_FOLDERS:
            typ, _ = M.select(f'"{folder}"', readonly=True)
            if typ != "OK":
                continue
            typ, data = M.search(None, "SINCE", since, "TEXT", utr)
            if typ != "OK" or not data or not data[0]:
                continue
            for num in data[0].split()[-20:]:
                typ, parts = M.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                msg = email.message_from_bytes(parts[0][1])
                res, reason = match_message(msg, utr, amount)
                if res is not None:
                    return {"found": True, "payer_hash": res["payer_hash"]}
                last_reason = reason
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return {"found": False, "reason": last_reason}


# --------------------------------------------------------------------------
#  Public: verify + activate
# --------------------------------------------------------------------------
MESSAGES = {
    "bad_plan": "Invalid plan.",
    "bad_utr": "UTR must be exactly 12 digits.",
    "utr_used": "This UTR has already been used.",
    "duplicate_ref": "This UTR has already been used.",
    "trial_used": "The ₹2 trial can be used only once per account.",
    "trial_payer_used": "The ₹2 trial was already used with this UPI account.",
    "trial_account_used": "This Telegram account has already used a ₹2 trial.",
    "already_paid": "You already have an active paid plan.",
    "too_fast": "Please wait a few seconds before trying again.",
    "not_found": "Payment not found yet. Wait 1–2 minutes and try again (check the amount and UTR).",
    "amount_mismatch": "Payment found but the amount does not match this plan.",
    "imap_not_configured": "Payment verification is not configured. Contact support.",
    "imap_error": "Could not check payments right now. Please try again in a minute.",
    "no_user": "Please open the bot with /start first.",
    "trial_upi_only": "The ₹2 trial is available only via UPI.",
}


def _err(reason: str) -> dict:
    return {"ok": False, "reason": reason, "message": MESSAGES.get(reason, "Verification failed. Contact support.")}


async def verify_and_activate_upi(user_id: int, plan_key: str, utr: str) -> dict:
    plan = config.PLANS.get(plan_key)
    if not plan or plan["inr"] <= 0:
        return _err("bad_plan")
    utr = re.sub(r"\s+", "", utr or "")
    if not UTR_RE.match(utr):
        return _err("bad_utr")
    now = time.monotonic()
    if now - _last_try.get(user_id, 0) < TRY_GAP_SEC:
        return _err("too_fast")
    _last_try[user_id] = now

    if await db.utr_used(utr):
        return _err("utr_used")
    if plan_key == "trial":
        u = await db.get_user(user_id)
        if not u:
            return _err("no_user")
        if not db.trial_eligible_row(u):
            return _err("already_paid" if db.effective_status(u) == "active" else "trial_used")

    amount = config.plan_amount_inr(plan_key)
    try:
        scan = await asyncio.to_thread(_imap_scan, utr, amount)
    except Exception as e:  # network / login error
        log.warning("IMAP error: %s", e)
        return _err("imap_error")
    if not scan["found"]:
        reason = scan.get("reason", "not_found")
        return _err("amount_mismatch" if reason == "amount_mismatch" else
                    "imap_not_configured" if reason == "imap_not_configured" else "not_found")

    res = await db.activate_utr_payment(user_id, plan_key, utr, amount, scan.get("payer_hash"))
    if not res["ok"]:
        return _err(res["reason"])
    res["message"] = f"{plan['name']} activated!"
    return res


# --------------------------------------------------------------------------
#  Future hook: Razorpay UPI AutoPay
# --------------------------------------------------------------------------
async def razorpay_autopay_hook(user_id: int, plan_key: str, event: dict) -> dict:
    """FUTURE: Razorpay UPI AutoPay webhook yahin jodna.

    Plan: webhook signature verify karo -> event me payment_id ko `ref` bana ke
    `db._activate(conn, user_id, plan_key, 'utr', 'rzp_' + payment_id, amount, None)` jaisa hi
    flow (database.activate_utr_payment) call karo, taaki duplicate guard aur trial locks wahi rahein.
    Abhi ye band hai.
    """
    return {"ok": False, "reason": "razorpay_not_enabled", "message": "Razorpay AutoPay is not enabled yet."}
