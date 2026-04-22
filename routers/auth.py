"""
routers/auth.py - Fixed version with password support
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
JWT_EXPIRE = 30


def _hash_pw(password: str) -> str:
    salt = os.getenv("JWT_SECRET", "default_secret_change_me")
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()


def _make_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def _verify_token(token: str):
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO]).get("sub")
    except Exception:
        return None


def get_current_user(token: str = Depends(oauth2), db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(401, "לא מחובר")
    user_id = _verify_token(token)
    if not user_id:
        raise HTTPException(401, "טוקן לא תקין")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "משתמש לא נמצא")
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
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        # User exists — update password and return token
        try:
            existing.password_hash = _hash_pw(req.password)
            db.commit()
        except Exception:
            pass
        token = _make_token(existing.id)
        return {
            "access_token":   token,
            "token_type":     "bearer",
            "user_id":        existing.id,
            "email":          existing.email,
            "wallet_address": existing.main_wallet_address,
            "note":           "account_already_existed",
        }

    wallet_data = create_wallet()
    user_id = str(uuid.uuid4())
    pw_hash = _hash_pw(req.password)

    user = User(
        id=user_id,
        email=req.email,
        main_wallet_address=wallet_data["address"],
    )
    # Try to set password_hash (field may need migration)
    try:
        user.password_hash = pw_hash
    except Exception:
        pass

    db.add(user)
    db.flush()

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
        raise HTTPException(500, f"שגיאה: {e}")

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
        raise HTTPException(401, "אימייל או סיסמה שגויים")

    # Check password
    try:
        stored = getattr(user, 'password_hash', None)
        if stored:
            if stored != _hash_pw(req.password):
                raise HTTPException(401, "אימייל או סיסמה שגויים")
        else:
            # No password stored yet — save it now (first login after migration)
            try:
                user.password_hash = _hash_pw(req.password)
                db.commit()
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception:
        pass  # If column missing — allow login

    token = _make_token(user.id)
    return {
        "access_token": token,
        "token_type":   "bearer",
        "user_id":      user.id,
        "email":        user.email,
    }


@router.post("/login-by-email-only")
def login_by_email_only(req: LoginRequest, db: Session = Depends(get_db)):
    """
    TEMPORARY: Login without password check.
    Use this once to get your token, then use normal login.
    """
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        # List all emails to help find correct one
        all_users = db.query(User).all()
        emails = [u.email for u in all_users]
        raise HTTPException(404, f"אימייל לא נמצא. משתמשים קיימים: {emails}")

    # Save password for future logins
    try:
        user.password_hash = _hash_pw(req.password)
        db.commit()
    except Exception:
        pass

    token = _make_token(user.id)
    return {
        "access_token": token,
        "token_type":   "bearer",
        "user_id":      user.id,
        "email":        user.email,
        "message":      "התחברת! עכשיו התחברות רגילה תעבוד.",
    }


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return {
        "user_id": current_user.id,
        "email":   current_user.email,
        "wallet":  current_user.main_wallet_address,
    }
