"""
routers/wallets.py
------------------
GET    /api/wallets/{user_id}                      — כל הארנקים של משתמש + יתרות
POST   /api/wallets/{user_id}/create               — צור ארנק חדש
PATCH  /api/wallets/{wallet_id}/label              — שנה שם ארנק
PATCH  /api/wallets/{wallet_id}/set-default        — קבע ברירת מחדל
POST   /api/wallets/transfer                       — העבר USDC בין ארנקים
GET    /api/wallets/{wallet_id}/balance            — רענן יתרה
GET    /api/wallets/{wallet_id}/approval-status    — בדוק אישורי חוזי Polymarket
POST   /api/wallets/{wallet_id}/approve-polymarket — אשר חוזי Polymarket (חד-פעמי)
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from db import get_db
from models.wallet import Wallet
from models.copy_settings import User
from services.wallet_service import (
    create_wallet, get_all_balances, transfer_usdc,
    approve_polymarket_contracts, check_polymarket_approval,
)
from datetime import datetime, timezone

router = APIRouter()


# ------------------------------------------------------------------ #
#  Schema                                                              #
# ------------------------------------------------------------------ #

class CreateWalletRequest(BaseModel):
    user_id: str
    label:   str = "ארנק חדש"
    set_as_default: bool = False


class TransferRequest(BaseModel):
    from_wallet_id: int
    to_wallet_id:   int
    amount_usdc:    float


class LabelRequest(BaseModel):
    label: str


class BackupExportRequest(BaseModel):
    password: str  # סיסמה לגיבוי — לא נשמרת בשרת, רק לגזירת מפתח AES


class BackupImportRequest(BaseModel):
    password: str
    backup:   dict  # הקובץ המוצפן כ-JSON


# ------------------------------------------------------------------ #
#  Endpoints                                                           #
# ------------------------------------------------------------------ #

@router.get("/{user_id}")
def list_wallets(user_id: str, db: Session = Depends(get_db)):
    """מחזיר את כל הארנקים של המשתמש עם יתרות מה-cache."""
    wallets = db.query(Wallet).filter(Wallet.user_id == user_id).all()

    return [
        {
            "id":             w.id,
            "address":        w.address,
            "label":          w.label,
            "is_default":     w.is_default,
            "usdc_balance":   w.cached_usdc_balance,
            "matic_balance":  w.cached_matic_balance,
            "balance_updated_at": w.balance_updated_at.isoformat() if w.balance_updated_at else None,
            "created_at":     w.created_at.isoformat(),
        }
        for w in wallets
    ]


@router.post("/{user_id}/recover")
def recover_wallet(user_id: str, db: Session = Depends(get_db)):
    """יוצר ארנק ראשי למשתמש שנרשם לפני תמיכת ארנקים."""
    existing = db.query(Wallet).filter(Wallet.user_id == user_id).first()
    if existing:
        return {"already_exists": True, "address": existing.address}

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_w = create_wallet()
    wallet = Wallet(
        user_id=user_id,
        address=new_w["address"],
        encrypted_private_key=new_w["encrypted_private_key"],
        label="ארנק ראשי",
        is_default=True,
        cached_usdc_balance=0.0,
    )
    user.main_wallet_address = new_w["address"]
    db.add(wallet)
    db.commit()
    db.refresh(wallet)

    return {
        "id":                 wallet.id,
        "address":            wallet.address,
        "private_key_backup": new_w["private_key_plaintext"],
        "warning": "שמור את ה-private key במקום בטוח! הוא לא יוצג שוב.",
    }


@router.post("/{user_id}/create")
def create_user_wallet(user_id: str, req: CreateWalletRequest, db: Session = Depends(get_db)):
    """יוצר ארנק Polygon חדש עבור המשתמש."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_w = create_wallet()

    # אם מבקשים ברירת מחדל — מאפסים את הישן
    if req.set_as_default:
        db.query(Wallet).filter(
            Wallet.user_id == user_id,
            Wallet.is_default == True,
        ).update({"is_default": False})

    wallet = Wallet(
        user_id=user_id,
        address=new_w["address"],
        encrypted_private_key=new_w["encrypted_private_key"],
        label=req.label,
        is_default=req.set_as_default,
        cached_usdc_balance=0.0,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)

    return {
        "id":                   wallet.id,
        "address":              wallet.address,
        "label":                wallet.label,
        "is_default":           wallet.is_default,
        # ⚠️ private key מוחזר רק פעם אחת — המשתמש חייב לשמור!
        "private_key_backup":   new_w["private_key_plaintext"],
        "warning": "שמור את ה-private key במקום בטוח! הוא לא יוצג שוב.",
    }


@router.patch("/{wallet_id}/label")
def update_label(wallet_id: int, req: LabelRequest, db: Session = Depends(get_db)):
    """שינוי שם תצוגה של ארנק."""
    w = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")
    w.label = req.label
    db.commit()
    return {"updated": True, "label": w.label}


@router.patch("/{wallet_id}/set-default")
def set_default_wallet(wallet_id: int, db: Session = Depends(get_db)):
    """קובע ארנק כברירת מחדל — מאפס את הישן."""
    w = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")

    # אפס ברירת מחדל ישנה
    db.query(Wallet).filter(
        Wallet.user_id == w.user_id,
        Wallet.is_default == True,
    ).update({"is_default": False})

    w.is_default = True
    db.commit()
    return {"updated": True, "default_wallet": w.address}


@router.post("/transfer")
def transfer_between_wallets(req: TransferRequest, db: Session = Depends(get_db)):
    """
    מעביר USDC מארנק אחד לאחר בתוך המערכת.
    שני הארנקים חייבים להיות של אותו משתמש.
    """
    from_w = db.query(Wallet).filter(Wallet.id == req.from_wallet_id).first()
    to_w   = db.query(Wallet).filter(Wallet.id == req.to_wallet_id).first()

    if not from_w or not to_w:
        raise HTTPException(status_code=404, detail="Wallet not found")

    # בטיחות — רק בין ארנקים של אותו משתמש
    if from_w.user_id != to_w.user_id:
        raise HTTPException(status_code=403, detail="Cannot transfer between different users")

    if req.amount_usdc <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    # בדוק יתרה
    if from_w.cached_usdc_balance < req.amount_usdc:
        raise HTTPException(
            status_code=400,
            detail=f"יתרה לא מספיקה. יתרה נוכחית: ${from_w.cached_usdc_balance:.2f}"
        )

    # בצע העברה
    try:
        result = transfer_usdc(
            from_encrypted_key=from_w.encrypted_private_key,
            from_address=from_w.address,
            to_address=to_w.address,
            amount_usdc=req.amount_usdc,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Transfer failed: {str(e)}")

    if not result["success"]:
        raise HTTPException(status_code=502, detail="Transaction reverted on-chain")

    # עדכן cache מיידית
    from_w.cached_usdc_balance -= req.amount_usdc
    to_w.cached_usdc_balance   += req.amount_usdc
    db.commit()

    return {
        "success":     True,
        "tx_hash":     result["tx_hash"],
        "from_wallet": from_w.address,
        "to_wallet":   to_w.address,
        "amount_usdc": req.amount_usdc,
        "from_new_balance": round(from_w.cached_usdc_balance, 2),
        "to_new_balance":   round(to_w.cached_usdc_balance,   2),
    }


@router.get("/{wallet_id}/approval-status")
async def get_approval_status(wallet_id: int, db: Session = Depends(get_db)):
    """
    בדיקה (read-only) — האם הארנק כבר אישר את חוזי Polymarket.
    אינו מוציא gas. מחזיר {ready: bool, ...} לכל חוזה.
    """
    w = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")
    result = await check_polymarket_approval(w.address)
    return {**result, "wallet_address": w.address, "wallet_id": wallet_id}


@router.post("/{wallet_id}/approve-polymarket")
async def approve_polymarket(wallet_id: int, db: Session = Depends(get_db)):
    """
    אישור חד-פעמי של כל חוזי Polymarket — נדרש לפני טריידינג אמיתי.
    מבצע עד 5 טרנזקציות Polygon. עלות: ~0.005-0.02 MATIC.
    חוזים שכבר אושרו — מדולגים (ללא gas).
    """
    w = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")
    if not w.encrypted_private_key:
        raise HTTPException(status_code=400, detail="אין מפתח פרטי לארנק זה")

    # Check MATIC balance first — need at least 0.005 MATIC for gas
    from services.wallet_service import get_matic_balance
    matic = get_matic_balance(w.address)
    if matic < 0.005:
        raise HTTPException(
            status_code=400,
            detail=f"יתרת MATIC לא מספיקה לעמלות gas. יתרה: {matic:.4f} MATIC. נדרש: לפחות 0.005 MATIC."
        )

    result = await approve_polymarket_contracts(w.address, w.encrypted_private_key)
    return {
        **result,
        "wallet_address": w.address,
        "wallet_id":      wallet_id,
        "matic_used":     matic - get_matic_balance(w.address),
    }


@router.get("/{wallet_id}/balance")
def refresh_balance(wallet_id: int, db: Session = Depends(get_db)):
    """מרענן יתרה מהבלוקצ'יין ומחזיר עדכון."""
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
        "label":        w.label,
        "usdc_balance": w.cached_usdc_balance,
        "matic_balance": w.cached_matic_balance,
        "updated_at":   w.balance_updated_at.isoformat(),
    }


# ------------------------------------------------------------------ #
#  Encrypted Backup / Restore                                          #
# ------------------------------------------------------------------ #

@router.post("/{user_id}/export-backup")
def export_backup(user_id: str, req: BackupExportRequest, db: Session = Depends(get_db)):
    """
    מייצא גיבוי מוצפן של כל הארנקים.
    ההצפנה: AES-256-GCM + PBKDF2-SHA256 (200K iterations).
    הסיסמה לא נשמרת בשרת — רק המשתמש יודע אותה.
    הקובץ המוצפן בטוח לשמירה בכל מקום, כולל GitHub ציבורי.
    """
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="סיסמה חייבת להיות לפחות 8 תווים")

    ws = db.query(Wallet).filter(Wallet.user_id == user_id).all()
    if not ws:
        raise HTTPException(status_code=404, detail="אין ארנקים לייצוא")

    from services.wallet_service import decrypt_private_key
    from services.wallet_backup  import encrypt_backup

    wallet_data = []
    for w in ws:
        pk = None
        if w.encrypted_private_key:
            try:
                pk = decrypt_private_key(w.encrypted_private_key)
            except Exception:
                pk = None   # ארנק ללא מפתח (ארנק חיצוני) — מייצא כתובת בלבד
        wallet_data.append({
            "address":    w.address,
            "private_key": pk,
            "label":      w.label,
            "is_default": w.is_default,
        })

    encrypted = encrypt_backup(wallet_data, req.password)
    encrypted["exported_at"]   = datetime.now(timezone.utc).isoformat()
    encrypted["wallet_count"]  = len(wallet_data)
    # user_id מוסף כמידע בלבד (לא חלק מהנתונים המוצפנים)
    encrypted["hint_user_id"]  = user_id

    return encrypted


@router.post("/{user_id}/import-backup")
def import_backup(user_id: str, req: BackupImportRequest, db: Session = Depends(get_db)):
    """
    משחזר ארנקים מגיבוי מוצפן.
    ארנקים שכבר קיימים (לפי כתובת) — מדולגים.
    ארנקים חדשים — נוצרים עם ה-private key המשוחזר.
    """
    from services.wallet_backup  import decrypt_backup
    from services.wallet_service import encrypt_private_key
    from cryptography.exceptions import InvalidTag

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        wallet_data = decrypt_backup(req.backup, req.password)
    except (InvalidTag, ValueError):
        raise HTTPException(status_code=400, detail="סיסמה שגויה או קובץ גיבוי פגום")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"שגיאה בפענוח: {str(e)}")

    results   = []
    restored  = 0

    for item in wallet_data:
        addr = item.get("address", "")
        if not addr:
            continue

        existing = db.query(Wallet).filter(Wallet.address == addr).first()
        if existing:
            results.append({"address": addr, "status": "already_exists", "label": item.get("label")})
            continue

        pk = item.get("private_key")
        if not pk:
            results.append({"address": addr, "status": "no_key_skipped", "label": item.get("label")})
            continue

        w = Wallet(
            user_id=user_id,
            address=addr,
            encrypted_private_key=encrypt_private_key(pk),
            label=item.get("label", "ארנק משוחזר"),
            is_default=item.get("is_default", False),
            cached_usdc_balance=0.0,
        )
        db.add(w)
        results.append({"address": addr, "status": "restored", "label": item.get("label")})
        restored += 1

    db.commit()
    return {
        "success":  True,
        "restored": restored,
        "skipped":  len(results) - restored,
        "details":  results,
    }
