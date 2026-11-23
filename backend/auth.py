"""Signed tokens (HMAC-SHA256). The server, not the browser, decides who a request belongs to.

- admin token:    {"kind": "admin", "agency_id", "email"}
- customer token: {"kind": "customer", "agency_id", "user_id"}

A customer can only ever reach threads and memories under their own (agency_id, user_id),
because both come from this signed token, never from the request body or the model.
"""
import base64
import hashlib
import hmac
import json
import time

from fastapi import Header, HTTPException

from backend import config


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign(payload: dict, ttl_hours: int | None = None) -> str:
    payload = {**payload, "exp": int(time.time()) + 3600 * (ttl_hours or config.TOKEN_TTL_HOURS)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def _bearer(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing token")
    payload = verify(authorization.split(" ", 1)[1].strip())
    if not payload:
        raise HTTPException(401, "Invalid or expired token")
    return payload


def require_admin(authorization: str | None = Header(default=None)) -> dict:
    payload = _bearer(authorization)
    if payload.get("kind") != "admin":
        raise HTTPException(403, "Admin token required")
    return payload


def require_customer(authorization: str | None = Header(default=None)) -> dict:
    payload = _bearer(authorization)
    if payload.get("kind") != "customer":
        raise HTTPException(403, "Customer token required")
    return payload
