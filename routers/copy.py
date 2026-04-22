"""
routers/copy.py — Copy Trading API Endpoints
החלף את הקובץ הקיים בזה
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import httpx

router = APIRouter()

DATA_API = "https://data-api.polymarket.com"

# ── Import models dynamically to avoid import errors ──────────────
def get_db():
    from db import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(token: str = None):
    """Simplified auth - adapt to your existing auth system"""
    from routers.auth import get_current_user as _get_user
    return _get_user

# ── Pydantic models ───────────────────────────────────────────────

class CopySettingCreate(BaseModel):
    trader_address: str
    wallet_id: int
    copy_mode: str = "fixed"        # fixed | percent | mirror
    fixed_amount: Optional[float]   = 10.0
    copy_percent: Optional[float]   = 10.0
    mirror_ratio: Optional[float]   = 1.0
    max_trade: Optional[float]      = 100.0
    max_daily: Optional[float]      = 500.0
    stop_loss: Optional[float]      = None
    is_demo: bool                   = True
    is_active: bool                 = True

class CopySettingUpdate(BaseModel):
    is_active: Optional[bool]   = None
    is_demo: Optional[bool]     = None
    fixed_amount: Optional[float] = None
    copy_percent: Optional[float] = None
    max_trade: Optional[float]  = None
    stop_loss: Optional[float]  = None

# ── Helpers ───────────────────────────────────────────────────────

def _get_copy_setting_model():
    try:
        from models.copy_settings import CopySetting
        return CopySetting
    except ImportError:
        try:
            from models import CopySetting
            return CopySetting
        except ImportError:
            return None

def _get_wallet_model():
    try:
        from models.wallet import Wallet
        return Wallet
    except ImportError:
        try:
            from models import Wallet
            return Wallet
        except ImportError:
            return None

# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/settings")
async def get_copy_settings(
    user_id: int = 1,   # TODO: replace with real auth
    db: Session = Depends(get_db)
):
    """Get all copy settings for user."""
    CopySetting = _get_copy_setting_model()
    if not CopySetting:
        return []
    return db.query(CopySetting).filter(
        CopySetting.user_id == user_id
    ).all()

@router.post("/settings")
async def create_copy_setting(
    data: CopySettingCreate,
    user_id: int = 1,
    db: Session = Depends(get_db)
):
    """Create a new copy trading configuration."""
    CopySetting = _get_copy_setting_model()
    Wallet = _get_wallet_model()

    if Wallet:
        wallet = db.query(Wallet).filter(
            Wallet.id == data.wallet_id
        ).first()
        if not wallet:
            raise HTTPException(404, "Wallet not found")

    if CopySetting:
        setting = CopySetting(
            user_id=user_id,
            trader_address=data.trader_address,
            wallet_id=data.wallet_id,
            copy_mode=data.copy_mode,
            fixed_amount=data.fixed_amount,
            copy_percent=data.copy_percent,
            max_trade=data.max_trade,
            max_daily=data.max_daily,
            stop_loss=data.stop_loss,
            is_demo=data.is_demo,
            is_active=data.is_active
        )
        db.add(setting)
        db.commit()
        db.refresh(setting)
        return setting

    return {"success": True, "trader_address": data.trader_address}

@router.patch("/settings/{setting_id}")
async def update_copy_setting(
    setting_id: int,
    data: CopySettingUpdate,
    db: Session = Depends(get_db)
):
    CopySetting = _get_copy_setting_model()
    if not CopySetting:
        raise HTTPException(404, "Model not available")
    setting = db.query(CopySetting).filter(CopySetting.id == setting_id).first()
    if not setting:
        raise HTTPException(404, "Setting not found")
    for field, value in data.dict(exclude_none=True).items():
        setattr(setting, field, value)
    db.commit()
    return setting

@router.delete("/settings/{setting_id}")
async def delete_copy_setting(
    setting_id: int,
    db: Session = Depends(get_db)
):
    CopySetting = _get_copy_setting_model()
    if CopySetting:
        setting = db.query(CopySetting).filter(CopySetting.id == setting_id).first()
        if setting:
            setting.is_active = False
            db.commit()
    return {"success": True}

@router.get("/history")
async def get_copy_history(
    user_id: int = 1,
    limit: int = 50,
    db: Session = Depends(get_db)
):
    """Get copy trade history."""
    try:
        # Try to import CopyTrade model
        try:
            from models.copy_trade import CopyTrade
        except ImportError:
            return []  # Table doesn't exist yet
        trades = db.query(CopyTrade).filter(
            CopyTrade.user_id == user_id
        ).order_by(CopyTrade.created_at.desc()).limit(limit).all()
        return [
            {
                "id": t.id,
                "side": t.side,
                "size": t.size,
                "price": t.price,
                "market": t.market_title,
                "status": t.status,
                "is_demo": t.is_demo,
                "trader": t.trader_address,
                "created_at": str(t.created_at) if t.created_at else None
            }
            for t in trades
        ]
    except Exception:
        return []

@router.get("/live/{trader_address}")
async def get_trader_live(trader_address: str):
    """
    Get LIVE recent activity of a trader from Polymarket.
    This is what the copy engine monitors.
    """
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                f"{DATA_API}/activity",
                params={"user": trader_address, "limit": 20},
                headers={"Accept": "application/json"}
            )
            if r.is_success:
                return {"activity": r.json(), "trader": trader_address}
            return {"activity": [], "trader": trader_address, "error": r.text}
    except Exception as e:
        raise HTTPException(502, str(e))

@router.get("/positions/{trader_address}")
async def get_trader_positions(trader_address: str):
    """Get current open positions of a trader."""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                f"{DATA_API}/positions",
                params={"user": trader_address, "limit": 100, "closed": "false"},
                headers={"Accept": "application/json"}
            )
            return r.json() if r.is_success else []
    except Exception as e:
        raise HTTPException(502, str(e))

@router.get("/stats")
async def get_copy_stats(user_id: int = 1, db: Session = Depends(get_db)):
    """Get overall copy trading stats."""
    try:
        from models.copy_trade import CopyTrade
        trades = db.query(CopyTrade).filter(CopyTrade.user_id == user_id).all()
        return {
            "total": len(trades),
            "demo": len([t for t in trades if t.is_demo]),
            "real": len([t for t in trades if not t.is_demo]),
            "volume": sum(t.size for t in trades)
        }
    except Exception:
        return {"total": 0, "demo": 0, "real": 0, "volume": 0}
