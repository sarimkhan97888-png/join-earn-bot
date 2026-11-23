"""
Profitix Ghost - outgoing webhooks (Pro).

Every new incoming message is POSTed as signed JSON to the user's endpoint:
  X-Ghost-Signature: sha256=<hex HMAC-SHA256 of the raw body using the user's secret>
The endpoint may answer with JSON to control the reply (only if "webhook decides the reply" is on):
  {"reply": "text to send"}   -> send this text instead of the saved rules
  {"skip": true}              -> do not reply
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("webhooks")

TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 65536


def _blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified
    )


async def validate_url(url: str) -> Tuple[bool, str]:
    """SSRF guard: public http(s) hosts only (no localhost / private / link-local addresses)."""
    url = (url or "").strip()
    if not url or len(url) > 500:
        return False, "Enter a webhook URL (max 500 characters)."
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False, "That URL is not valid."
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False, "URL must start with https:// and include a host."
    port = port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        return False, "Could not resolve that host."
    for info in infos:
        if _blocked_ip(info[4][0]):
            return False, "Private, local and reserved addresses are not allowed."
    return True, ""


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_event(
    platform: str, owner_id: int, sender_id: int, chat_id: int, msg_id: int, text: str,
    name: Optional[str] = None, username: Optional[str] = None, event_name: str = "message.incoming",
) -> dict:
    return {
        "event": event_name,
        "platform": platform,
        "user_id": owner_id,
        "message": {
            "id": msg_id,
            "text": (text or "")[:4000],
            "chat_id": chat_id,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        "from": {"id": sender_id, "name": name, "username": username},
    }


async def post_event(url: str, secret: str, event: dict) -> dict:
    """Returns {"ok": bool, "status": int|None, "error": str, "json": dict|None}. Never raises."""
    ok, why = await validate_url(url)
    if not ok:
        return {"ok": False, "status": None, "error": why, "json": None}
    body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ProfitixGhost/1.0",
        "X-Ghost-Event": str(event.get("event", "message.incoming")),
        "X-Ghost-Timestamp": str(int(time.time())),
    }
    if secret:
        headers["X-Ghost-Signature"] = sign(secret, body)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS), follow_redirects=False) as client:
            resp = await client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "status": None, "error": exc.__class__.__name__, "json": None}

    good = 200 <= resp.status_code < 300
    data = None
    if good and "json" in resp.headers.get("content-type", "") and len(resp.content) <= MAX_RESPONSE_BYTES:
        try:
            parsed = resp.json()
            data = parsed if isinstance(parsed, dict) else None
        except ValueError:
            data = None
    return {"ok": good, "status": resp.status_code, "error": "" if good else f"HTTP {resp.status_code}", "json": data}
