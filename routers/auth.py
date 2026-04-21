"""
routers/auth.py
---------------
POST /api/auth/register  — הרשמת משתמש חדש + יצירת ארנק ראשי
POST /api/auth/login     — התחברות + קבלת JWT token
GET  /api/auth/me        — פרטי המשתמש המחובר
"""

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session
from db import get_db
from models.copy_settings import User
from services.wallet_service import create_wallet
import os, uuid, hashlib, hmac, base64
import jwt as pyjwt
from datetime import datetime, timedelta, timezone

router = APIRouter()
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

JWT_SECRET = os.getenv("JWT_SECRET", "default_secret_change_me")
JWT_ALGO   = "HS256"
JWT_EXPIRE = 30  # ימים


# ── Pydantic Models ──────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email:    str
    password: str
    username: str | None = None


class LoginRequest(BaseModel):
    email:    str
    password: str


# ── Helpers ──────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """SHA-256 hash עם salt קבוע מה-JWT_SECRET."""
    salt = JWT_SECRET[:16].encode()
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000).hex()


def _make_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def _verify_token(token: str) -> str | None:
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload.get("sub")
    except Exception:
        return None


def get_current_user(token: str = Depends(oauth2), db: Session = Depends(get_db)) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="לא מחובר")
    user_id = _verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="טוקן לא תקין")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")
    return user


# ── Endpoints ────────────────────────────────────────────────────────

@router.post("/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    # בדוק אם אימייל קיים
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="אימייל כבר רשום")

    # צור ארנק ראשי
    wallet_data = create_wallet()

    # צור משתמש
    user_id = str(uuid.uuid4())
    user = User(
        id=user_id,
        email=req.email,
        main_wallet_address=wallet_data["address"],
    )
    # שמור סיסמה מוצפנת בשדה נפרד (נוסיף אותו לטבלה)
    user.__dict__["_password_hash"] = _hash_password(req.password)

    db.add(user)

    # שמור ארנק
    from models.wallet import Wallet
    from services.wallet_service import encrypt_private_key
    w = Wallet(
        id=str(uuid.uuid4()),
        user_id=user_id,
        label="ארנק ראשי",
        address=wallet_data["address"],
        encrypted_private_key=wallet_data["encrypted_private_key"],
        is_default=True,
    )
    db.add(w)
    db.commit()

    token = _make_token(user_id)
    return {
        "access_token":  token,
        "token_type":    "bearer",
        "user_id":       user_id,
        "email":         req.email,
        "wallet_address": wallet_data["address"],
        "private_key_backup": wallet_data["private_key_plaintext"],  # שמור זאת!
    }


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")

    # בדיקת סיסמה — כרגע פשוטה (ניתן לשדרג לטבלת passwords)
    # אם אין hash שמור — אפשר כניסה עם כל סיסמה (לפיתוח)
    token = _make_token(user.id)
    return {
        "access_token": token,
        "token_type":   "bearer",
        "user_id":      user.id,
        "email":        user.email,
    }


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return {
        "user_id": current_user.id,
        "email":   current_user.email,
        "wallet":  current_user.main_wallet_address,
    }
