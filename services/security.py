"""
services/security.py
--------------------
כל שכבות האבטחה של המערכת:

1. JWT — אימות משתמש בכל בקשה
2. Rate Limiting — הגבלת בקשות
3. Withdrawal Whitelist — רק לכתובות מאושרות מראש
4. TOTP 2FA — אימות דו-שלבי לפני משיכה
5. הצפנת private keys (קיים ב-wallet_service)
"""

import os
import time
import hmac
import hashlib
import base64
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from collections import defaultdict

import jwt
import pyotp
from fastapi import HTTPException, Header, Depends
from sqlalchemy.orm import Session
from db import get_db

JWT_SECRET  = os.getenv("JWT_SECRET",  secrets.token_hex(32))
JWT_ALGO    = "HS256"
JWT_EXPIRE  = 24   # שעות


# ------------------------------------------------------------------ #
#  1. JWT — יצירה ואימות טוקן                                          #
# ------------------------------------------------------------------ #

def create_access_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def verify_token(token: str) -> str:
    """מחזיר user_id אם הטוקן תקין, זורק HTTPException אחרת."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user(authorization: str = Header(...)) -> str:
    """
    FastAPI Dependency — שים ב-endpoint:
        user_id: str = Depends(get_current_user)
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    return verify_token(authorization[7:])


# ------------------------------------------------------------------ #
#  2. Rate Limiting — בזיכרון (לפרודקשן: Redis)                        #
# ------------------------------------------------------------------ #

_rate_store: dict[str, list[float]] = defaultdict(list)

def check_rate_limit(key: str, max_requests: int = 60, window_seconds: int = 60):
    """
    מגביל כמות בקשות לפי key (למשל IP או user_id).
    max_requests: מקסימום בקשות בחלון זמן.
    window_seconds: גודל החלון בשניות.
    """
    now = time.time()
    window_start = now - window_seconds

    # נקה בקשות ישנות
    _rate_store[key] = [t for t in _rate_store[key] if t > window_start]

    if len(_rate_store[key]) >= max_requests:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Max {max_requests} per {window_seconds}s.",
        )
    _rate_store[key].append(now)


# ------------------------------------------------------------------ #
#  3. Withdrawal Whitelist — רשימת כתובות מאושרות                      #
# ------------------------------------------------------------------ #

def validate_withdrawal_address(user_id: str, to_address: str, db: Session) -> bool:
    """
    בודק שהכתובת שמבקשים למשוך אליה נמצאת ברשימה המאושרת של המשתמש.
    אם הרשימה ריקה — מותר לכל כתובת (רלוונטי רק אם המשתמש הגדיר whitelist).
    """
    from models.withdrawal_whitelist import WithdrawalWhitelist
    whitelist = db.query(WithdrawalWhitelist).filter(
        WithdrawalWhitelist.user_id == user_id
    ).all()

    if not whitelist:
        return True   # לא הוגדר whitelist — מותר לכל כתובת

    approved = {w.address.lower() for w in whitelist}
    return to_address.lower() in approved


# ------------------------------------------------------------------ #
#  4. TOTP 2FA — אימות דו-שלבי                                         #
# ------------------------------------------------------------------ #

def generate_totp_secret() -> str:
    """מייצר secret חדש ל-2FA. יש לשמור ב-DB ולהצגה ב-QR."""
    return pyotp.random_base32()


def get_totp_uri(secret: str, user_email: str) -> str:
    """מחזיר URI לסריקת QR (Google Authenticator / Authy)."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=user_email, issuer_name="PolyTrade")


def verify_totp(secret: str, code: str) -> bool:
    """מאמת קוד 6-ספרתי מה-Authenticator. חלון ±30 שניות."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def require_2fa(secret: Optional[str], code: Optional[str]):
    """
    Dependency — בדוק 2FA לפני פעולות רגישות (משיכה, שינוי הגדרות).
    אם המשתמש לא הגדיר 2FA — מדלג.
    """
    if not secret:
        return   # 2FA לא מופעל — מאפשר
    if not code:
        raise HTTPException(status_code=403, detail="2FA code required")
    if not verify_totp(secret, code):
        raise HTTPException(status_code=403, detail="Invalid 2FA code")
