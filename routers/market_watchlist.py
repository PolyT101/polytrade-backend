"""
routers/market_watchlist.py
----------------------------
GET    /api/market-watchlist/{user_id}         — רשימת שאלות שמורות
POST   /api/market-watchlist/{user_id}/add     — שמור שאלה
DELETE /api/market-watchlist/{user_id}/{cid}   — הסר שאלה
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from db import get_db
from models.market_watchlist import MarketWatchlistEntry
from datetime import datetime, timezone

router = APIRouter()


class AddMarketRequest(BaseModel):
    condition_id:   str
    slug:           Optional[str]   = None
    question:       str
    category:       Optional[str]   = None
    yes_price:      Optional[float] = None
    no_price:       Optional[float] = None
    volume:         Optional[float] = None
    days_remaining: Optional[int]   = None
    source_page:    Optional[str]   = None   # מאיפה נשמרה


@router.get("/{user_id}")
def get_market_watchlist(user_id: str, db: Session = Depends(get_db)):
    entries = db.query(MarketWatchlistEntry).filter(
        MarketWatchlistEntry.user_id == user_id
    ).order_by(MarketWatchlistEntry.added_at.desc()).all()

    return [
        {
            "id":             e.id,
            "condition_id":   e.condition_id,
            "slug":           e.slug,
            "question":       e.question,
            "category":       e.category,
            "yes_price":      e.yes_price,
            "no_price":       e.no_price,
            "volume":         e.volume,
            "days_remaining": e.days_remaining,
            "source_page":    e.source_page,
            "polymarket_url": e.polymarket_url,
            "added_at":       e.added_at.isoformat(),
        }
        for e in entries
    ]


@router.post("/{user_id}/add")
def add_to_market_watchlist(user_id: str, req: AddMarketRequest, db: Session = Depends(get_db)):
    existing = db.query(MarketWatchlistEntry).filter(
        MarketWatchlistEntry.user_id == user_id,
        MarketWatchlistEntry.condition_id == req.condition_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Market already in watchlist")

    entry = MarketWatchlistEntry(
        user_id=user_id,
        condition_id=req.condition_id,
        slug=req.slug,
        question=req.question,
        category=req.category,
        yes_price=req.yes_price,
        no_price=req.no_price,
        volume=req.volume,
        days_remaining=req.days_remaining,
        source_page=req.source_page,
        polymarket_url=f"https://polymarket.com/event/{req.slug}" if req.slug else None,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"status": "added", "id": entry.id}


@router.delete("/{user_id}/{condition_id}")
def remove_from_market_watchlist(user_id: str, condition_id: str, db: Session = Depends(get_db)):
    entry = db.query(MarketWatchlistEntry).filter(
        MarketWatchlistEntry.user_id == user_id,
        MarketWatchlistEntry.condition_id == condition_id,
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Not in watchlist")
    db.delete(entry)
    db.commit()
    return {"status": "removed"}


@router.get("/{user_id}/ids")
def get_watchlist_ids(user_id: str, db: Session = Depends(get_db)):
    """מחזיר רק את ה-condition_ids — לשימוש בפרונטאנד לסימון אייקון שמירה."""
    entries = db.query(MarketWatchlistEntry.condition_id).filter(
        MarketWatchlistEntry.user_id == user_id
    ).all()
    return {"ids": [e.condition_id for e in entries]}
