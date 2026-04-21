"""
routers/portfolio.py
--------------------
GET /api/portfolio/{user_id}                    — כל הקופי הפעיל
GET /api/portfolio/{user_id}/{trader_addr}/open  — עסקאות פתוחות
GET /api/portfolio/{user_id}/{trader_addr}/closed — עסקאות סגורות
POST /api/portfolio/sell/{trade_id}             — מכור עסקה ידנית
"""

from fastapi import APIRouter, HTTPException, Depends
from db import get_db
from models.copy_settings import CopySettings, CopyTrade
from services.trading_service import cancel_order
from sqlalchemy.orm import Session
from datetime import datetime, timezone

router = APIRouter()


@router.get("/{user_id}")
def get_portfolio(user_id: str, db: Session = Depends(get_db)):
    """מחזיר את כל הטריידרים שהמשתמש עושה מהם קופי + סטטיסטיקות מהירות."""
    settings = db.query(CopySettings).filter(
        CopySettings.user_id == user_id
    ).all()

    result = []
    for s in settings:
        trades = db.query(CopyTrade).filter(
            CopyTrade.user_id == user_id,
            CopyTrade.trader_address == s.trader_address,
        ).all()

        total_cost   = sum(t.cost_usdc or 0 for t in trades)
        total_pnl    = sum(t.pnl_usd  or 0 for t in trades)
        roi_pct      = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        open_count   = sum(1 for t in trades if t.status == "open")
        closed_count = sum(1 for t in trades if t.status == "closed")

        duration_days = (datetime.now(timezone.utc) - s.started_at).days

        result.append({
            "copy_id":       s.id,
            "trader_address": s.trader_address,
            "trader_name":   s.trader_name,
            "is_active":     s.is_active,
            "is_demo":       s.is_demo,
            "roi_pct":       round(roi_pct, 2),
            "total_pnl":     round(total_pnl, 2),
            "total_invested": round(total_cost, 2),
            "open_trades":   open_count,
            "closed_trades": closed_count,
            "duration_days": duration_days,
            "started_at":    s.started_at.isoformat(),
        })

    return result


@router.get("/{user_id}/{trader_address}/open")
def get_open_trades(user_id: str, trader_address: str, db: Session = Depends(get_db)):
    trades = db.query(CopyTrade).filter(
        CopyTrade.user_id == user_id,
        CopyTrade.trader_address == trader_address,
        CopyTrade.status == "open",
    ).order_by(CopyTrade.opened_at.desc()).all()
    return trades


@router.get("/{user_id}/{trader_address}/closed")
def get_closed_trades(user_id: str, trader_address: str, db: Session = Depends(get_db)):
    trades = db.query(CopyTrade).filter(
        CopyTrade.user_id == user_id,
        CopyTrade.trader_address == trader_address,
        CopyTrade.status == "closed",
    ).order_by(CopyTrade.closed_at.desc()).all()
    return trades


@router.post("/sell/{trade_id}")
def sell_trade(trade_id: int, db: Session = Depends(get_db)):
    """מכירה ידנית של עסקה פתוחה."""
    trade = db.query(CopyTrade).filter(CopyTrade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != "open":
        raise HTTPException(status_code=400, detail="Trade is not open")

    if not trade.is_demo and trade.order_id:
        user = trade.user
        try:
            cancel_order(
                private_key=user.encrypted_private_key,  # decrypt בפרודקשן
                funder_address=user.wallet_address,
                order_id=trade.order_id,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Cancel failed: {e}")

    trade.status    = "closed"
    trade.closed_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "sold", "trade_id": trade_id}
