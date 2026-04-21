"""
routers/watchlist.py
--------------------
GET    /api/watchlist/{user_id}              — רשימת טריידרים שמורים
POST   /api/watchlist/{user_id}/add          — הוסף טריידר למעקב
DELETE /api/watchlist/{user_id}/{trader_addr} — הסר טריידר
PATCH  /api/watchlist/{entry_id}/notes       — הוסף הערה אישית
GET    /api/watchlist/{user_id}/{trader_addr}/activity — עסקאות אחרונות
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from db import get_db
from models.watchlist import WatchlistEntry
from models.copy_settings import CopySettings
from services.polymarket_service import get_trader_profile, get_trader_positions
from datetime import datetime, timezone

router = APIRouter()


class AddToWatchlistRequest(BaseModel):
    trader_address: str
    trader_name:    str
    notes:          Optional[str] = None


class NotesRequest(BaseModel):
    notes: str


@router.get("/{user_id}")
def get_watchlist(user_id: str, db: Session = Depends(get_db)):
    """
    מחזיר את רשימת הטריידרים השמורים עם:
    - הנתונים השמורים (pnl, roi וכד׳)
    - האם יש קופי פעיל מהטריידר הזה
    """
    entries = db.query(WatchlistEntry).filter(
        WatchlistEntry.user_id == user_id
    ).order_by(WatchlistEntry.added_at.desc()).all()

    # בדוק אילו טריידרים יש מהם קופי פעיל
    active_copies = {
        s.trader_address
        for s in db.query(CopySettings).filter(
            CopySettings.user_id == user_id,
            CopySettings.is_active == True,
        ).all()
    }

    return [
        {
            "id":              e.id,
            "trader_address":  e.trader_address,
            "trader_name":     e.trader_name,
            "pnl":             e.pnl,
            "roi":             e.roi,
            "win_rate":        e.win_rate,
            "trades_count":    e.trades_count,
            "style":           e.style,
            "notes":           e.notes,
            "is_copying":      e.trader_address in active_copies,  # קופי פעיל?
            "added_at":        e.added_at.isoformat(),
            "last_checked_at": e.last_checked_at.isoformat() if e.last_checked_at else None,
        }
        for e in entries
    ]


@router.post("/{user_id}/add")
async def add_to_watchlist(
    user_id: str,
    req: AddToWatchlistRequest,
    db: Session = Depends(get_db),
):
    """
    מוסיף טריידר לרשימת המעקב.
    מושך אוטומטית את הנתונים העדכניים שלו מפולימארקט.
    """
    # בדוק שלא כבר קיים
    existing = db.query(WatchlistEntry).filter(
        WatchlistEntry.user_id == user_id,
        WatchlistEntry.trader_address == req.trader_address,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Trader already in watchlist")

    # מושך נתונים עדכניים מפולימארקט
    pnl, roi, win_rate, trades_count, style = None, None, None, None, None
    try:
        profile = await get_trader_profile(req.trader_address)
        pnl         = profile.get("pnl")
        roi         = profile.get("roi")
        win_rate    = profile.get("winRate")
        trades_count = profile.get("tradesCount")
        style       = profile.get("style")
    except Exception:
        pass   # אם נכשל — שומר בלי נתונים, אפשר לרענן אחר כך

    entry = WatchlistEntry(
        user_id=user_id,
        trader_address=req.trader_address,
        trader_name=req.trader_name,
        pnl=str(pnl) if pnl else None,
        roi=str(roi) if roi else None,
        win_rate=str(win_rate) if win_rate else None,
        trades_count=trades_count,
        style=style,
        notes=req.notes,
        last_checked_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    return {
        "status":         "added",
        "id":             entry.id,
        "trader_name":    entry.trader_name,
        "trader_address": entry.trader_address,
    }


@router.delete("/{user_id}/{trader_address}")
def remove_from_watchlist(user_id: str, trader_address: str, db: Session = Depends(get_db)):
    entry = db.query(WatchlistEntry).filter(
        WatchlistEntry.user_id == user_id,
        WatchlistEntry.trader_address == trader_address,
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Not in watchlist")
    db.delete(entry)
    db.commit()
    return {"status": "removed", "trader_address": trader_address}


@router.patch("/{entry_id}/notes")
def update_notes(entry_id: int, req: NotesRequest, db: Session = Depends(get_db)):
    """הוספת / עדכון הערה אישית על טריידר."""
    entry = db.query(WatchlistEntry).filter(WatchlistEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    entry.notes = req.notes
    db.commit()
    return {"updated": True}


@router.get("/{user_id}/{trader_address}/activity")
async def get_trader_activity(user_id: str, trader_address: str, db: Session = Depends(get_db)):
    """
    מחזיר את העסקאות האחרונות של טריידר ממעקב — לצפייה מהירה.
    גם מעדכן את הנתונים השמורים עליו.
    """
    entry = db.query(WatchlistEntry).filter(
        WatchlistEntry.user_id == user_id,
        WatchlistEntry.trader_address == trader_address,
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Trader not in watchlist")

    try:
        profile   = await get_trader_profile(trader_address)
        positions = await get_trader_positions(trader_address, closed=False)

        # עדכן נתונים שמורים
        entry.pnl           = str(profile.get("pnl", entry.pnl))
        entry.roi           = str(profile.get("roi", entry.roi))
        entry.win_rate      = str(profile.get("winRate", entry.win_rate))
        entry.trades_count  = profile.get("tradesCount", entry.trades_count)
        entry.last_checked_at = datetime.now(timezone.utc)
        db.commit()

        return {
            "trader_address": trader_address,
            "trader_name":    entry.trader_name,
            "profile":        profile,
            "open_positions": positions,
            "checked_at":     entry.last_checked_at.isoformat(),
        }

    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch activity: {e}")


@router.post("/{user_id}/{trader_address}/refresh")
async def refresh_trader_data(user_id: str, trader_address: str, db: Session = Depends(get_db)):
    """מרענן את הנתונים השמורים של טריידר ממעקב."""
    entry = db.query(WatchlistEntry).filter(
        WatchlistEntry.user_id == user_id,
        WatchlistEntry.trader_address == trader_address,
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Trader not in watchlist")

    try:
        profile = await get_trader_profile(trader_address)
        entry.pnl          = str(profile.get("pnl",        entry.pnl))
        entry.roi          = str(profile.get("roi",        entry.roi))
        entry.win_rate     = str(profile.get("winRate",    entry.win_rate))
        entry.trades_count = profile.get("tradesCount",    entry.trades_count)
        entry.style        = profile.get("style",          entry.style)
        entry.last_checked_at = datetime.now(timezone.utc)
        db.commit()
        return {"status": "refreshed", "trader_name": entry.trader_name}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
