"""
routers/copy.py — Copy Trading API
מותאם למבנה המודלים הקיים
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import httpx

router = APIRouter()

DATA_API = "https://data-api.polymarket.com"


class CopySettingIn(BaseModel):
    trader_address:   str
    trader_name:      str = ""
    wallet_address:   Optional[str] = None
    entry_mode:       str   = "fixed"   # fixed | percent
    entry_amount:     float = 50.0
    take_profit_pct:  Optional[float] = None
    stop_loss_pct:    Optional[float] = None
    max_daily_trades: Optional[int]   = None
    max_daily_loss_usd: Optional[float] = None
    sell_mode:        str = "mirror"    # mirror | fixed | manual | sell_all
    is_active:        bool = True


# ── /settings ────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings(user_id: str = "1"):
    try:
        from db import SessionLocal
        from models.copy_settings import CopySettings
        db = SessionLocal()
        try:
            rows = db.query(CopySettings).filter(
                CopySettings.user_id == user_id,
                CopySettings.is_active == True
            ).all()
            return [_fmt_setting(r) for r in rows]
        finally:
            db.close()
    except Exception as e:
        return []


@router.post("/settings")
async def create_setting(data: CopySettingIn, user_id: str = "1"):
    try:
        from db import SessionLocal
        from models.copy_settings import CopySettings
        db = SessionLocal()
        try:
            # Check duplicate
            existing = db.query(CopySettings).filter(
                CopySettings.user_id == user_id,
                CopySettings.trader_address == data.trader_address,
                CopySettings.is_active == True
            ).first()
            if existing:
                raise HTTPException(400, "כבר עושה קופי לטריידר זה")

            s = CopySettings(
                user_id=user_id,
                trader_address=data.trader_address,
                trader_name=data.trader_name,
                wallet_address=data.wallet_address,
                entry_mode=data.entry_mode,
                entry_amount=data.entry_amount,
                take_profit_pct=data.take_profit_pct,
                stop_loss_pct=data.stop_loss_pct,
                max_daily_trades=data.max_daily_trades,
                max_daily_loss_usd=data.max_daily_loss_usd,
                sell_mode=data.sell_mode,
                is_active=data.is_active,
            )
            db.add(s)
            db.commit()
            db.refresh(s)
            return _fmt_setting(s)
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/settings/{setting_id}")
async def stop_copy(setting_id: int, user_id: str = "1"):
    try:
        from db import SessionLocal
        from models.copy_settings import CopySettings
        db = SessionLocal()
        try:
            s = db.query(CopySettings).filter(
                CopySettings.id == setting_id,
                CopySettings.user_id == user_id
            ).first()
            if not s:
                raise HTTPException(404, "לא נמצא")
            s.is_active = False
            db.commit()
            return {"success": True}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── /history ─────────────────────────────────────────────────────

@router.get("/history")
async def get_history(user_id: str = "1", limit: int = 50):
    try:
        from db import SessionLocal
        from models.copy_settings import CopyTrade
        db = SessionLocal()
        try:
            trades = db.query(CopyTrade).filter(
                CopyTrade.user_id == user_id
            ).order_by(CopyTrade.opened_at.desc()).limit(limit).all()
            return [_fmt_trade(t) for t in trades]
        finally:
            db.close()
    except Exception as e:
        return []


@router.get("/stats")
async def get_stats(user_id: str = "1"):
    try:
        from db import SessionLocal
        from models.copy_settings import CopyTrade
        db = SessionLocal()
        try:
            trades = db.query(CopyTrade).filter(
                CopyTrade.user_id == user_id
            ).all()
            open_t   = [t for t in trades if t.status == "open"]
            closed_t = [t for t in trades if t.status == "closed"]
            demo_t   = [t for t in trades if t.status == "demo"]
            total_pnl = sum(t.pnl_usd or 0 for t in closed_t)
            return {
                "total": len(trades),
                "open": len(open_t),
                "closed": len(closed_t),
                "demo": len(demo_t),
                "total_pnl": round(total_pnl, 2),
                "total_volume": round(sum(t.amount_usdc or 0 for t in trades), 2),
            }
        finally:
            db.close()
    except Exception as e:
        return {"total": 0, "open": 0, "closed": 0, "demo": 0,
                "total_pnl": 0, "total_volume": 0}


# ── Live data from Polymarket ────────────────────────────────────

@router.get("/live/{trader_address}")
async def get_trader_live(trader_address: str):
    """פעילות אחרונה של טריידר — בזמן אמת מ-Polymarket."""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                f"{DATA_API}/activity",
                params={"user": trader_address, "limit": 20},
                headers={"Accept": "application/json"}
            )
            return {
                "activity": r.json() if r.is_success else [],
                "trader": trader_address
            }
    except Exception as e:
        raise HTTPException(502, str(e))


@router.get("/positions/{trader_address}")
async def get_trader_positions(trader_address: str):
    """פוזיציות פתוחות של טריידר."""
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


# ── Helpers ───────────────────────────────────────────────────────

def _fmt_setting(s) -> dict:
    return {
        "id":               s.id,
        "trader_address":   s.trader_address,
        "trader_name":      s.trader_name,
        "entry_mode":       s.entry_mode,
        "entry_amount":     s.entry_amount,
        "take_profit_pct":  s.take_profit_pct,
        "stop_loss_pct":    s.stop_loss_pct,
        "max_daily_trades": s.max_daily_trades,
        "max_daily_loss":   s.max_daily_loss_usd,
        "sell_mode":        s.sell_mode,
        "is_active":        s.is_active,
        "created_at":       str(s.created_at) if s.created_at else None,
    }

def _fmt_trade(t) -> dict:
    return {
        "id":            t.id,
        "trader":        t.trader_address,
        "market":        t.market_question,
        "market_id":     t.market_id,
        "side":          t.side,
        "amount":        t.amount_usdc,
        "price_entry":   t.price_entry,
        "price_exit":    t.price_exit,
        "pnl":           t.pnl_usd,
        "status":        t.status,
        "tx_hash":       t.tx_hash,
        "opened_at":     str(t.opened_at) if t.opened_at else None,
        "closed_at":     str(t.closed_at) if t.closed_at else None,
    }
