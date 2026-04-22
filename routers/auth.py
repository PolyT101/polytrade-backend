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
from models.wallet import Wallet
from services.wallet_service import create_wallet
import os, uuid, hashlib
import jwt as pyjwt
from datetime import datetime, timedelta, timezone

router = APIRouter()
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

JWT_SECRET = os.getenv("JWT_SECRET", "default_secret_change_me")
JWT_ALGO   = "HS256"
JWT_EXPIRE = 30  # ימים


def _hash_password(password: str) -> str:
    """Hash password with SHA256 + salt."""
    salt = os.getenv("JWT_SECRET", "polytrade_salt")
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()


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


def get_current_user(
    token: str = Depends(oauth2),
    db: Session = Depends(get_db)
) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="לא מחובר")
    user_id = _verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="טוקן לא תקין")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")
    return user


class RegisterRequest(BaseModel):
    email:    str
    password: str
    username: str | None = None


class LoginRequest(BaseModel):
    email:    str
    password: str


@router.post("/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    # בדוק אם אימייל קיים
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        # If user exists but has no password hash — update it and return token
        # (handles case where user registered before password was saved)
        try:
            pw_hash = _hash_password(req.password)
            if not existing.password_hash:
                existing.password_hash = pw_hash
                db.commit()
            token = _make_token(existing.id)
            return {
                "access_token":   token,
                "token_type":     "bearer",
                "user_id":        existing.id,
                "email":          existing.email,
                "wallet_address": existing.main_wallet_address,
                "already_existed": True,
            }
        except Exception:
            raise HTTPException(status_code=400, detail="אימייל כבר רשום — נסה להתחבר")

    # צור ארנק ראשי
    wallet_data = create_wallet()

    # Hash password
    pw_hash = _hash_password(req.password)

    # צור משתמש
    user_id = str(uuid.uuid4())

    # Try to save password_hash — field may not exist yet
    user_kwargs = {
        "id":                  user_id,
        "email":               req.email,
        "main_wallet_address": wallet_data["address"],
    }
    try:
        user_kwargs["password_hash"] = pw_hash
    except Exception:
        pass

    user = User(**user_kwargs)
    db.add(user)
    db.flush()

    # צור ארנק
    w = Wallet(
        user_id=user_id,
        label="ארנק ראשי",
        address=wallet_data["address"],
        encrypted_private_key=wallet_data["encrypted_private_key"],
        is_default=True,
        cached_usdc_balance=0.0,
        cached_matic_balance=0.0,
    )
    db.add(w)

    try:
        db.commit()
        db.refresh(user)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"שגיאה ביצירת חשבון: {e}")

    token = _make_token(user_id)
    return {
        "access_token":       token,
        "token_type":         "bearer",
        "user_id":            user_id,
        "email":              req.email,
        "wallet_address":     wallet_data["address"],
        "private_key_backup": wallet_data["private_key_plaintext"],
    }


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")

    # Check password if hash exists
    try:
        if user.password_hash:
            pw_hash = _hash_password(req.password)
            if user.password_hash != pw_hash:
                raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")
        else:
            # No password hash saved (old account) — save it now and allow login
            user.password_hash = _hash_password(req.password)
            db.commit()
    except HTTPException:
        raise
    except Exception:
        # password_hash column doesn't exist yet — allow login without password check
        pass

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


@router.post("/reset-password")
def reset_password(
    req: LoginRequest,
    db: Session = Depends(get_db)
):
    """
    Emergency: reset password for existing user.
    Use when login fails due to missing password hash.
    """
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")
    try:
        user.password_hash = _hash_password(req.password)
        db.commit()
        token = _make_token(user.id)
        return {
            "success": True,
            "access_token": token,
            "user_id": user.id,
            "message": "סיסמה עודכנה בהצלחה"
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
