"""
routers/dashboard.py
--------------------
עמוד הדשבורד — ניהול ארנקים מרכזי

GET    /api/dashboard/{user_id}                  — סקירה כללית + כל הארנקים
POST   /api/dashboard/{user_id}/wallets/create   — פתח ארנק חדש
PATCH  /api/dashboard/wallets/{wallet_id}/label  — שנה שם
PATCH  /api/dashboard/wallets/{wallet_id}/default — קבע ברירת מחדל
POST   /api/dashboard/transfer/internal          — העבר בין ארנקים פנימיים
POST   /api/dashboard/transfer/withdraw          — משוך החוצה (עם 2FA)
POST   /api/dashboard/whitelist/add              — הוסף כתובת לרשימה המאושרת
DELETE /api/dashboard/whitelist/{wl_id}          — הסר כתובת
GET    /api/dashboard/{user_id}/whitelist         — רשימת כתובות מאושרות
POST   /api/dashboard/{user_id}/2fa/setup        — הפעל 2FA
POST   /api/dashboard/{user_id}/2fa/verify       — אמת קוד 2FA
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from db import get_db
from models.wallet import Wallet
from models.copy_settings import User
from models.withdrawal_whitelist import WithdrawalWhitelist
from services.wallet_service import (
    create_wallet, get_all_balances, transfer_usdc
)
from services.security import (
    validate_withdrawal_address, require_2fa,
    generate_totp_secret, get_totp_uri, verify_totp,
)
from datetime import datetime, timezone

router = APIRouter()


# ------------------------------------------------------------------ #
#  Schema                                                              #
# ------------------------------------------------------------------ #

class CreateWalletReq(BaseModel):
    label:          str  = "ארנק חדש"
    set_as_default: bool = False


class InternalTransferReq(BaseModel):
    from_wallet_id: int
    to_wallet_id:   int
    amount_usdc:    float


class WithdrawReq(BaseModel):
    from_wallet_id: int
    to_address:     str     # כתובת חיצונית (MetaMask / כל ארנק)
    amount_usdc:    float
    totp_code:      Optional[str] = None   # חובה אם 2FA מופעל


class WhitelistReq(BaseModel):
    address: str
    label:   Optional[str] = None


class LabelReq(BaseModel):
    label: str


class Setup2FAReq(BaseModel):
    email: str


class Verify2FAReq(BaseModel):
    code: str


# ------------------------------------------------------------------ #
#  Dashboard Overview                                                  #
# ------------------------------------------------------------------ #

@router.get("/{user_id}")
def get_dashboard(user_id: str, db: Session = Depends(get_db)):
    """
    מחזיר:
    - כל הארנקים עם יתרות (מה-cache)
    - סה"כ USDC בכל הארנקים
    - מספר קופי פעילים
    - האם 2FA מופעל
    """
    wallets = db.query(Wallet).filter(Wallet.user_id == user_id).all()
    user    = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    wallet_list = [
        {
            "id":             w.id,
            "address":        w.address,
            "label":          w.label,
            "is_default":     w.is_default,
            "usdc_balance":   round(w.cached_usdc_balance or 0, 2),
            "matic_balance":  round(w.cached_matic_balance or 0, 4),
            "balance_updated_at": w.balance_updated_at.isoformat() if w.balance_updated_at else None,
            "created_at":     w.created_at.isoformat(),
        }
        for w in wallets
    ]

    total_usdc = sum(w["usdc_balance"] for w in wallet_list)

    from models.copy_settings import CopySettings
    active_copies = db.query(CopySettings).filter(
        CopySettings.user_id == user_id,
        CopySettings.is_active == True,
    ).count()

    return {
        "user_id":       user_id,
        "wallets":       wallet_list,
        "total_usdc":    round(total_usdc, 2),
        "wallet_count":  len(wallet_list),
        "active_copies": active_copies,
        "has_2fa":       bool(getattr(user, "totp_secret", None)),
    }


# ------------------------------------------------------------------ #
#  ניהול ארנקים                                                        #
# ------------------------------------------------------------------ #

@router.post("/{user_id}/wallets/create")
def create_user_wallet(user_id: str, req: CreateWalletReq, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_w = create_wallet()

    if req.set_as_default:
        db.query(Wallet).filter(
            Wallet.user_id == user_id, Wallet.is_default == True
        ).update({"is_default": False})

    w = Wallet(
        user_id=user_id,
        address=new_w["address"],
        encrypted_private_key=new_w["encrypted_private_key"],
        label=req.label,
        is_default=req.set_as_default,
        cached_usdc_balance=0.0,
    )
    db.add(w)
    db.commit()
    db.refresh(w)

    return {
        "id":                 w.id,
        "address":            w.address,
        "label":              w.label,
        "is_default":         w.is_default,
        "private_key_backup": new_w["private_key_plaintext"],
        "warning":            "⚠️ שמור את ה-private key במקום בטוח! הוא לא יוצג שוב.",
        "deposit_info":       f"שלח USDC (Polygon) לכתובת: {w.address}",
    }


@router.patch("/wallets/{wallet_id}/label")
def update_wallet_label(wallet_id: int, req: LabelReq, db: Session = Depends(get_db)):
    w = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")
    w.label = req.label
    db.commit()
    return {"updated": True, "label": w.label}


@router.patch("/wallets/{wallet_id}/default")
def set_default_wallet(wallet_id: int, db: Session = Depends(get_db)):
    w = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")
    db.query(Wallet).filter(
        Wallet.user_id == w.user_id, Wallet.is_default == True
    ).update({"is_default": False})
    w.is_default = True
    db.commit()
    return {"updated": True, "default_wallet": w.address}


@router.get("/wallets/{wallet_id}/refresh-balance")
def refresh_balance(wallet_id: int, db: Session = Depends(get_db)):
    w = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")
    balances = get_all_balances(w.address)
    w.cached_usdc_balance  = balances["usdc_balance"]
    w.cached_matic_balance = balances["matic_balance"]
    w.balance_updated_at   = datetime.now(timezone.utc)
    db.commit()
    return {
        "address":      w.address,
        "usdc_balance": w.cached_usdc_balance,
        "matic_balance": w.cached_matic_balance,
    }


# ------------------------------------------------------------------ #
#  העברות                                                              #
# ------------------------------------------------------------------ #

@router.post("/transfer/internal")
def internal_transfer(req: InternalTransferReq, db: Session = Depends(get_db)):
    """העברת USDC בין שני ארנקים פנימיים."""
    from_w = db.query(Wallet).filter(Wallet.id == req.from_wallet_id).first()
    to_w   = db.query(Wallet).filter(Wallet.id == req.to_wallet_id).first()

    if not from_w or not to_w:
        raise HTTPException(status_code=404, detail="Wallet not found")
    if from_w.user_id != to_w.user_id:
        raise HTTPException(status_code=403, detail="Cannot transfer between different users")
    if req.amount_usdc <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if (from_w.cached_usdc_balance or 0) < req.amount_usdc:
        raise HTTPException(
            status_code=400,
            detail=f"יתרה לא מספיקה. יתרה: ${from_w.cached_usdc_balance:.2f}"
        )

    result = transfer_usdc(
        from_encrypted_key=from_w.encrypted_private_key,
        from_address=from_w.address,
        to_address=to_w.address,
        amount_usdc=req.amount_usdc,
    )

    if not result["success"]:
        raise HTTPException(status_code=502, detail="Transaction failed on-chain")

    from_w.cached_usdc_balance = (from_w.cached_usdc_balance or 0) - req.amount_usdc
    to_w.cached_usdc_balance   = (to_w.cached_usdc_balance   or 0) + req.amount_usdc
    db.commit()

    return {
        "success":          True,
        "tx_hash":          result["tx_hash"],
        "from_wallet":      from_w.label,
        "to_wallet":        to_w.label,
        "amount_usdc":      req.amount_usdc,
        "from_new_balance": round(from_w.cached_usdc_balance, 2),
        "to_new_balance":   round(to_w.cached_usdc_balance,   2),
    }


@router.post("/transfer/withdraw")
def external_withdraw(req: WithdrawReq, db: Session = Depends(get_db)):
    """
    משיכה לכתובת חיצונית (MetaMask / כל ארנק אחר).
    
    אבטחה:
    1. בודק שהכתובת ברשימת ה-whitelist (אם הוגדרה)
    2. בודק 2FA (אם מופעל)
    3. מבצע העברה על הבלוקצ'יין
    """
    from_w = db.query(Wallet).filter(Wallet.id == req.from_wallet_id).first()
    if not from_w:
        raise HTTPException(status_code=404, detail="Wallet not found")

    user = db.query(User).filter(User.id == from_w.user_id).first()

    # ---- בדיקת Whitelist ----
    if not validate_withdrawal_address(from_w.user_id, req.to_address, db):
        raise HTTPException(
            status_code=403,
            detail="כתובת זו אינה ברשימת הכתובות המאושרות שלך. הוסף אותה תחילה."
        )

    # ---- בדיקת 2FA ----
    totp_secret = getattr(user, "totp_secret", None)
    require_2fa(totp_secret, req.totp_code)

    # ---- בדיקת יתרה ----
    if (from_w.cached_usdc_balance or 0) < req.amount_usdc:
        raise HTTPException(
            status_code=400,
            detail=f"יתרה לא מספיקה. יתרה: ${from_w.cached_usdc_balance:.2f}"
        )

    # ---- ביצוע ----
    result = transfer_usdc(
        from_encrypted_key=from_w.encrypted_private_key,
        from_address=from_w.address,
        to_address=req.to_address,
        amount_usdc=req.amount_usdc,
    )

    if not result["success"]:
        raise HTTPException(status_code=502, detail="Transaction failed on-chain")

    from_w.cached_usdc_balance = (from_w.cached_usdc_balance or 0) - req.amount_usdc
    db.commit()

    return {
        "success":       True,
        "tx_hash":       result["tx_hash"],
        "to_address":    req.to_address,
        "amount_usdc":   req.amount_usdc,
        "new_balance":   round(from_w.cached_usdc_balance, 2),
        "polygon_scan":  f"https://polygonscan.com/tx/{result['tx_hash']}",
    }


# ------------------------------------------------------------------ #
#  Whitelist                                                           #
# ------------------------------------------------------------------ #

@router.get("/{user_id}/whitelist")
def get_whitelist(user_id: str, db: Session = Depends(get_db)):
    entries = db.query(WithdrawalWhitelist).filter(
        WithdrawalWhitelist.user_id == user_id,
        WithdrawalWhitelist.is_active == True,
    ).all()
    return [
        {"id": e.id, "address": e.address, "label": e.label, "added_at": e.added_at.isoformat()}
        for e in entries
    ]


@router.post("/{user_id}/whitelist/add")
def add_to_whitelist(user_id: str, req: WhitelistReq, db: Session = Depends(get_db)):
    existing = db.query(WithdrawalWhitelist).filter(
        WithdrawalWhitelist.user_id == user_id,
        WithdrawalWhitelist.address == req.address.lower(),
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Address already whitelisted")

    entry = WithdrawalWhitelist(
        user_id=user_id,
        address=req.address.lower(),
        label=req.label,
    )
    db.add(entry)
    db.commit()
    return {"status": "added", "address": req.address}


@router.delete("/whitelist/{wl_id}")
def remove_from_whitelist(wl_id: int, db: Session = Depends(get_db)):
    entry = db.query(WithdrawalWhitelist).filter(WithdrawalWhitelist.id == wl_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Not found")
    entry.is_active = False
    db.commit()
    return {"status": "removed"}


# ------------------------------------------------------------------ #
#  2FA                                                                 #
# ------------------------------------------------------------------ #

@router.post("/{user_id}/2fa/setup")
def setup_2fa(user_id: str, req: Setup2FAReq, db: Session = Depends(get_db)):
    """מחזיר QR URI לסריקה ב-Authenticator."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    secret = generate_totp_secret()
    user.totp_secret = secret   # שמור ב-DB (צריך להוסיף עמודה)
    db.commit()

    return {
        "secret":  secret,
        "qr_uri":  get_totp_uri(secret, req.email),
        "message": "סרוק את ה-QR ב-Google Authenticator / Authy, ואז אמת עם קוד",
    }


@router.post("/{user_id}/2fa/verify")
def verify_2fa_setup(user_id: str, req: Verify2FAReq, db: Session = Depends(get_db)):
    """מאמת שה-2FA הוגדר נכון לאחר הסריקה."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not getattr(user, "totp_secret", None):
        raise HTTPException(status_code=404, detail="2FA not set up")

    if not verify_totp(user.totp_secret, req.code):
        raise HTTPException(status_code=403, detail="Invalid code — try again")

    return {"verified": True, "message": "2FA הופעל בהצלחה!"}
