"""
routers/copy.py — v3
עם endpoint לסגירת פוזיציה ידנית
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import httpx

router = APIRouter()
DATA_API = "https://data-api.polymarket.com"


class CopySettingIn(BaseModel):
    trader_address:    str
    trader_name:       str = ""
    wallet_address:    Optional[str] = None
    entry_mode:        str   = "fixed"
    entry_amount:      float = 10.0
    take_profit_pct:   Optional[float] = None
    stop_loss_pct:     Optional[float] = None
    max_daily_trades:  Optional[int]   = None
    max_daily_loss_usd: Optional[float] = None
    sell_mode:         str = "mirror"
    is_active:         bool = True
    reset_watermark:   bool = False


def _db():
    from db import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/settings")
async def get_settings(user_id: str = "1"):
    from db import SessionLocal
    from models.copy_settings import CopySettings
    db = SessionLocal()
    try:
        rows = db.query(CopySettings).filter(
            CopySettings.user_id == user_id
        ).all()
        return [_fmt_setting(r) for r in rows]
    except Exception as e:
        return []
    finally:
        db.close()


@router.post("/settings")
async def create_setting(data: CopySettingIn, user_id: str = "1"):
    from db import SessionLocal
    from models.copy_settings import CopySettings
    db = SessionLocal()
    try:
        # בדוק כפילות
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.delete("/settings/{setting_id}")
async def stop_copy(setting_id: int, user_id: str = "1"):
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.put("/settings/{setting_id}")
async def update_setting(setting_id: int, data: CopySettingIn, user_id: str = "1"):
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
        address_changed = data.trader_address and data.trader_address != s.trader_address
        if address_changed:
            s.trader_address = data.trader_address
        s.entry_mode         = data.entry_mode
        s.entry_amount       = data.entry_amount
        s.take_profit_pct    = data.take_profit_pct
        s.stop_loss_pct      = data.stop_loss_pct
        s.max_daily_trades   = data.max_daily_trades
        s.max_daily_loss_usd = data.max_daily_loss_usd
        s.sell_mode          = data.sell_mode
        if data.trader_name:
            s.trader_name = data.trader_name
        db.commit()
        if address_changed or data.reset_watermark:
            from models.copy_settings import CopyEngineState
            db.query(CopyEngineState).filter(
                CopyEngineState.setting_id == setting_id
            ).delete()
            db.commit()
        db.refresh(s)
        return _fmt_setting(s)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.post("/settings/{setting_id}/activate")
async def resume_copy(setting_id: int, user_id: str = "1"):
    """הפעלה מחדש של קופי שהופסק."""
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
        s.is_active = True
        db.commit()
        return {"success": True, "setting_id": setting_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.get("/history")
async def get_history(user_id: str = "1", limit: int = 500,
                      setting_id: int = None):
    from db import SessionLocal
    from models.copy_settings import CopyTrade
    db = SessionLocal()
    try:
        q = db.query(CopyTrade).filter(CopyTrade.user_id == user_id)
        if setting_id:
            q = q.filter(CopyTrade.copy_settings_id == setting_id)
        trades = q.order_by(CopyTrade.opened_at.desc()).limit(limit).all()
        return [_fmt_trade(t) for t in trades]
    except Exception:
        return []
    finally:
        db.close()


@router.post("/history/close")
async def close_position(data: dict):
    """סגירת פוזיציה ידנית מה-Frontend."""
    from db import SessionLocal
    from models.copy_settings import CopyTrade
    db = SessionLocal()
    try:
        trade_id = data.get("trade_id")
        if trade_id:
            t = db.query(CopyTrade).filter(CopyTrade.id == trade_id).first()
        else:
            # מצא לפי market_id + setting_id
            t = db.query(CopyTrade).filter(
                CopyTrade.user_id == data.get("user_id"),
                CopyTrade.copy_settings_id == data.get("copy_setting_id"),
                CopyTrade.market_id == data.get("market_id"),
                CopyTrade.status == "demo"
            ).first()

        if t:
            from datetime import datetime, timezone
            t.price_exit = float(data.get("price_exit", 0))
            t.pnl_usd    = float(data.get("pnl_usd", 0))
            t.status     = "closed"
            t.closed_at  = datetime.now(timezone.utc)
            db.commit()
            return {"success": True, "trade_id": t.id, "pnl": t.pnl_usd}
        return {"success": False, "error": "trade not found"}
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.get("/stats")
async def get_stats(user_id: str = "1"):
    from db import SessionLocal
    from models.copy_settings import CopyTrade
    db = SessionLocal()
    try:
        trades = db.query(CopyTrade).filter(CopyTrade.user_id == user_id).all()
        demo   = [t for t in trades if t.status == "demo"]
        closed = [t for t in trades if t.status == "closed"]
        return {
            "total":  len(trades),
            "demo":   len(demo),
            "closed": len(closed),
            "total_pnl":    round(sum(t.pnl_usd or 0 for t in closed), 2),
            "total_volume": round(sum(t.amount_usdc or 0 for t in trades), 2),
        }
    except Exception:
        return {"total": 0, "demo": 0, "closed": 0, "total_pnl": 0, "total_volume": 0}
    finally:
        db.close()


@router.post("/cleanup-duplicates")
async def cleanup_duplicates(user_id: str = "1"):
    """
    מחק duplicate demo trades ועסקאות של settings לא פעילים.
    שומר רק עסקאות של settings פעילים + עסקאות סגורות.
    """
    from db import SessionLocal
    from models.copy_settings import CopyTrade, CopySettings
    db = SessionLocal()
    try:
        # Get active setting IDs
        active_ids = set(
            s.id for s in db.query(CopySettings).filter(
                CopySettings.user_id == user_id,
                CopySettings.is_active == True
            ).all()
        )

        # Delete demo trades from inactive settings OR with no setting_id
        q = db.query(CopyTrade).filter(
            CopyTrade.user_id == user_id,
            CopyTrade.status == "demo",
        )
        if active_ids:
            from sqlalchemy import or_
            q = q.filter(or_(
                CopyTrade.copy_settings_id == None,          # null = old/orphan trades
                CopyTrade.copy_settings_id.notin_(active_ids) # inactive setting trades
            ))
        else:
            q = q.filter(CopyTrade.copy_settings_id == None)

        deleted_inactive = q.delete(synchronize_session=False)

        # Also dedup by tx_hash within active settings
        if active_ids:
            trades = db.query(CopyTrade).filter(
                CopyTrade.user_id == user_id,
                CopyTrade.status == "demo",
                CopyTrade.copy_settings_id.in_(active_ids),
            ).order_by(CopyTrade.id.asc()).all()
            seen = set()
            deleted_dups = 0
            for t in trades:
                key = (t.tx_hash or str(t.id), t.copy_settings_id)
                if t.tx_hash and key in seen:
                    db.delete(t)
                    deleted_dups += 1
                else:
                    seen.add(key)
        else:
            deleted_dups = 0

        db.commit()
        remaining = db.query(CopyTrade).filter(CopyTrade.user_id == user_id).count()
        return {
            "deleted_inactive": deleted_inactive,
            "deleted_duplicates": deleted_dups,
            "remaining": remaining
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.get("/debug/engine")
async def debug_engine(user_id: str = "1"):
    """אבחון מנוע הקופי — מראה watermark ופעילות אחרונה לכל setting."""
    from db import SessionLocal
    from models.copy_settings import CopySettings, CopyEngineState, CopyTrade
    from datetime import datetime, timezone
    db = SessionLocal()
    try:
        settings = db.query(CopySettings).filter(
            CopySettings.user_id == user_id,
            CopySettings.is_active == True
        ).all()
        result = []
        PM_H = {"Accept": "application/json", "Origin": "https://polymarket.com",
                "Referer": "https://polymarket.com/", "User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=10) as client:
            for s in settings:
                state = db.query(CopyEngineState).filter(
                    CopyEngineState.setting_id == s.id
                ).first()
                wm = int(state.last_seen_ts) if state and state.last_seen_ts else 0
                wm_dt = datetime.fromtimestamp(wm, tz=timezone.utc).isoformat() if wm else None
                r = await client.get(f"{DATA_API}/activity",
                    params={"user": s.trader_address, "limit": 10}, headers=PM_H)
                activity = r.json() if r.is_success and isinstance(r.json(), list) else []
                new_count = len([t for t in activity if int(t.get("timestamp", 0)) > wm])
                open_cnt  = db.query(CopyTrade).filter(
                    CopyTrade.copy_settings_id == s.id, CopyTrade.status == "demo").count()
                result.append({
                    "setting_id":   s.id,
                    "trader":       s.trader_name or s.trader_address[:12],
                    "watermark_dt": wm_dt,
                    "open_positions": open_cnt,
                    "pending_new_trades": new_count,
                    "last_5_activity": [
                        {"ts": a.get("timestamp"), "type": a.get("type"),
                         "side": a.get("side"), "price": a.get("price"),
                         "is_new": int(a.get("timestamp", 0)) > wm,
                         "title": (a.get("title") or "")[:35]}
                        for a in activity[:5]
                    ]
                })
        return result
    finally:
        db.close()


@router.get("/live/{trader_address}")
async def get_trader_live(trader_address: str):
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                f"{DATA_API}/activity",
                params={"user": trader_address, "limit": 20},
                headers={"Accept": "application/json"}
            )
            return {
                "activity": r.json() if r.is_success else [],
                "trader":   trader_address
            }
    except Exception as e:
        raise HTTPException(502, str(e))


@router.get("/positions/{trader_address}")
async def get_trader_positions(trader_address: str):
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                f"{DATA_API}/positions",
                params={"user": trader_address, "limit": 200, "closed": "false"},
                headers={"Accept": "application/json"}
            )
            return r.json() if r.is_success else []
    except Exception as e:
        raise HTTPException(502, str(e))


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
        "id":               t.id,
        "trader":           t.trader_address,
        "copy_settings_id": t.copy_settings_id,
        "market":           t.market_question,
        "market_id":        t.market_id,
        "side":             t.side,
        "amount":           t.amount_usdc,
        "price_entry":      t.price_entry,
        "price_exit":       t.price_exit,
        "pnl":              t.pnl_usd,
        "status":           t.status,
        "tx_hash":          t.tx_hash,
        "opened_at":        str(t.opened_at) if t.opened_at else None,
        "closed_at":        str(t.closed_at) if t.closed_at else None,
    }
