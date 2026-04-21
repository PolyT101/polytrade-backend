"""
routers/copy.py — גרסה 2
-------------------------
POST /api/copy/start        — התחל קופי (עם בחירת ארנק)
POST /api/copy/stop/{id}    — עצור קופי
POST /api/copy/resume/{id}  — חזור לקופי
GET  /api/copy/settings/{user_id}
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from db import get_db
from models.copy_settings import CopySettings, User
from models.wallet import Wallet
from sqlalchemy.orm import Session
from datetime import datetime, timezone

router = APIRouter()


class StartCopyRequest(BaseModel):
    user_id:              str
    trader_address:       str
    trader_name:          str

    # ---- בחירת ארנק ----
    # אפשרות א: use_default_wallet=True → ישתמש בארנק ברירת המחדל
    # אפשרות ב: wallet_id=<id> → ישתמש בארנק ספציפי קיים
    # אפשרות ג: שניהם False/None → יצור ארנק חדש
    use_default_wallet:   bool          = False
    wallet_id:            Optional[int] = None    # ID של ארנק קיים

    # ---- דמו ----
    is_demo:              bool          = False
    demo_balance_usd:     float         = 1000.0

    # ---- הגדרות כניסה ----
    mode:                 str           = "fixed"
    fixed_amount_usd:     float         = 50.0
    percentage:           float         = 5.0
    max_per_trade_usd:    float         = 100.0

    # ---- מגבלות יומיות (אופציונלי) ----
    max_daily_trades:     Optional[int]   = None
    max_daily_loss_usd:   Optional[float] = None
    max_daily_profit_usd: Optional[float] = None

    # ---- Take Profit / Stop Loss (אופציונלי) ----
    take_profit_pct:      Optional[float] = None
    stop_loss_pct:        Optional[float] = None

    # ---- מצב יציאה ----
    sell_mode:            str           = "mirror"


@router.post("/start")
def start_copy(req: StartCopyRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    existing = db.query(CopySettings).filter(
        CopySettings.user_id == req.user_id,
        CopySettings.trader_address == req.trader_address,
        CopySettings.is_active == True,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already copying this trader")

    # ---- בחירת / יצירת ארנק ----
    private_key_backup = None   # מוחזר רק אם נוצר ארנק חדש

    if req.use_default_wallet:
        # השתמש בארנק ברירת המחדל הקיים
        wallet = db.query(Wallet).filter(
            Wallet.user_id == req.user_id,
            Wallet.is_default == True,
        ).first()
        if not wallet:
            raise HTTPException(
                status_code=404,
                detail="לא נמצא ארנק ברירת מחדל — צור ארנק ראשון"
            )
        wallet_address = wallet.address
        wallet_encrypted_key = wallet.encrypted_private_key
        wallet_label = wallet.label

    elif req.wallet_id:
        # השתמש בארנק ספציפי שנבחר
        wallet = db.query(Wallet).filter(
            Wallet.id == req.wallet_id,
            Wallet.user_id == req.user_id,
        ).first()
        if not wallet:
            raise HTTPException(status_code=404, detail="Wallet not found")
        wallet_address = wallet.address
        wallet_encrypted_key = wallet.encrypted_private_key
        wallet_label = wallet.label

    else:
        # צור ארנק חדש ייעודי לקופי הזה
        from services.wallet_service import create_wallet
        new_w = create_wallet()
        wallet_address = new_w["address"]
        wallet_encrypted_key = new_w["encrypted_private_key"]
        wallet_label = f"קופי — {req.trader_name}"
        private_key_backup = new_w["private_key_plaintext"]

        # שמור כארנק חדש ב-DB
        new_wallet_row = Wallet(
            user_id=req.user_id,
            address=wallet_address,
            encrypted_private_key=wallet_encrypted_key,
            label=wallet_label,
            is_default=False,
        )
        db.add(new_wallet_row)
        db.flush()   # שמור לפני ה-commit כדי לקבל ID

    setting = CopySettings(
        user_id=req.user_id,
        trader_address=req.trader_address,
        trader_name=req.trader_name,
        copy_wallet_address=wallet_address,
        copy_wallet_encrypted_key=wallet_encrypted_key,
        is_active=True,
        is_demo=req.is_demo,
        demo_balance_usd=req.demo_balance_usd,
        mode=req.mode,
        fixed_amount_usd=req.fixed_amount_usd,
        percentage=req.percentage,
        max_per_trade_usd=req.max_per_trade_usd,
        max_daily_trades=req.max_daily_trades,
        max_daily_loss_usd=req.max_daily_loss_usd,
        max_daily_profit_usd=req.max_daily_profit_usd,
        take_profit_pct=req.take_profit_pct,
        stop_loss_pct=req.stop_loss_pct,
        sell_mode=req.sell_mode,
    )
    db.add(setting)
    db.commit()
    db.refresh(setting)

    response = {
        "status":         "started",
        "copy_id":        setting.id,
        "wallet_address": wallet_address,
        "wallet_label":   wallet_label,
    }

    if private_key_backup:
        response["private_key_backup"] = private_key_backup
        response["warning"] = "⚠️ שמור את ה-private key! הוא לא יוצג שוב."
        response["instructions"] = f"העבר USDC (Polygon) לכתובת {wallet_address} כדי להתחיל"

    return response


@router.post("/stop/{copy_id}")
def stop_copy(copy_id: int, db: Session = Depends(get_db)):
    s = db.query(CopySettings).filter(CopySettings.id == copy_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Copy setting not found")
    s.is_active = False
    db.commit()
    return {"status": "stopped", "copy_id": copy_id}


@router.post("/resume/{copy_id}")
def resume_copy(copy_id: int, db: Session = Depends(get_db)):
    s = db.query(CopySettings).filter(CopySettings.id == copy_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Copy setting not found")
    s.is_active = True
    db.commit()
    return {"status": "resumed", "copy_id": copy_id}


@router.get("/settings/{user_id}")
def get_copy_settings(user_id: str, db: Session = Depends(get_db)):
    return db.query(CopySettings).filter(CopySettings.user_id == user_id).all()
