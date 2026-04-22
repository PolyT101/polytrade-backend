"""
routers/copy.py — Copy Trading API Endpoints
=============================================
Endpoints for managing copy settings and viewing copy history.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from db import get_db
from models import CopySetting, CopyTrade, Wallet
from routers.auth import get_current_user
import httpx

router = APIRouter()

DATA_API = "https://data-api.polymarket.com"

# ── Request models ──────────────────────────────────────

class CopySettingCreate(BaseModel):
    trader_address: str
    wallet_id: int
    copy_mode: str = "fixed"       # fixed | percent | mirror
    fixed_amount: Optional[float] = 10.0
    copy_percent: Optional[float] = 10.0
    mirror_ratio: Optional[float] = 1.0
    max_trade: Optional[float] = 100.0
    max_daily: Optional[float] = 500.0
    stop_loss: Optional[float] = None
    is_demo: bool = True
    is_active: bool = True

class CopySettingUpdate(BaseModel):
    is_active: Optional[bool] = None
    is_demo: Optional[bool] = None
    fixed_amount: Optional[float] = None
    copy_percent: Optional[float] = None
    max_trade: Optional[float] = None
    stop_loss: Optional[float] = None

# ── Endpoints ────────────────────────────────────────────

@router.get("/settings")
async def get_copy_settings(
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get all copy settings for current user."""
    settings = db.query(CopySetting).filter(
        CopySetting.user_id == user.id
    ).all()
    return settings

@router.post("/settings")
async def create_copy_setting(
    data: CopySettingCreate,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Create a new copy trading configuration."""
    # Verify wallet belongs to user
    wallet = db.query(Wallet).filter(
        Wallet.id == data.wallet_id,
        Wallet.user_id == user.id
    ).first()
    if not wallet:
        raise HTTPException(404, "Wallet not found")

    # Check for duplicate
    existing = db.query(CopySetting).filter(
        CopySetting.user_id == user.id,
        CopySetting.trader_address == data.trader_address,
        CopySetting.wallet_id == data.wallet_id,
        CopySetting.is_active == True
    ).first()
    if existing:
        raise HTTPException(400, "Already copying this trader")

    setting = CopySetting(
        user_id=user.id,
        trader_address=data.trader_address,
        wallet_id=data.wallet_id,
        copy_mode=data.copy_mode,
        fixed_amount=data.fixed_amount,
        copy_percent=data.copy_percent,
        mirror_ratio=data.mirror_ratio,
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

@router.patch("/settings/{setting_id}")
async def update_copy_setting(
    setting_id: int,
    data: CopySettingUpdate,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Update a copy setting."""
    setting = db.query(CopySetting).filter(
        CopySetting.id == setting_id,
        CopySetting.user_id == user.id
    ).first()
    if not setting:
        raise HTTPException(404, "Setting not found")

    for field, value in data.dict(exclude_none=True).items():
        setattr(setting, field, value)

    db.commit()
    return setting

@router.delete("/settings/{setting_id}")
async def delete_copy_setting(
    setting_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Stop copying a trader."""
    setting = db.query(CopySetting).filter(
        CopySetting.id == setting_id,
        CopySetting.user_id == user.id
    ).first()
    if not setting:
        raise HTTPException(404, "Setting not found")
    setting.is_active = False
    db.commit()
    return {"success": True}

@router.get("/history")
async def get_copy_history(
    limit: int = 50,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get copy trade history."""
    trades = db.query(CopyTrade).filter(
        CopyTrade.user_id == user.id
    ).order_by(CopyTrade.created_at.desc()).limit(limit).all()
    return trades

@router.get("/live/{trader_address}")
async def get_trader_live_activity(trader_address: str):
    """
    Get LIVE recent activity of a specific trader.
    Used by frontend to show what the trader is doing right now.
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
            return {"activity": [], "trader": trader_address}
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

@router.get("/stats/{setting_id}")
async def get_copy_stats(
    setting_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get performance stats for a copy setting."""
    trades = db.query(CopyTrade).filter(
        CopyTrade.copy_setting_id == setting_id,
        CopyTrade.user_id == user.id
    ).all()

    total_trades = len(trades)
    demo_trades = [t for t in trades if t.is_demo]
    real_trades = [t for t in trades if not t.is_demo]

    return {
        "total_trades": total_trades,
        "demo_trades": len(demo_trades),
        "real_trades": len(real_trades),
        "total_volume": sum(t.size for t in trades),
        "trades": [
            {
                "id": t.id,
                "side": t.side,
                "size": t.size,
                "price": t.price,
                "market": t.market_title,
                "status": t.status,
                "is_demo": t.is_demo,
                "created_at": t.created_at.isoformat() if t.created_at else None
            }
            for t in trades[-20:]  # Last 20
        ]
    }
