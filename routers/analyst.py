"""
routers/analyst.py
------------------
GET /api/analyst/sniper        — הזדמנויות ROI גבוה
GET /api/analyst/safe-profit   — הסתברות >80%
GET /api/analyst/whales        — השקעות לוויתנים
"""

from fastapi import APIRouter, Query, HTTPException
from services.market_analyzer import (
    get_sniper_opportunities,
    get_safe_profit_markets,
    get_whale_markets,
)

router = APIRouter()


@router.get("/sniper")
async def sniper(limit: int = Query(50, ge=1, le=100)):
    """
    הזדמנויות עם ROI פוטנציאלי גבוה + זיהוי כסף חכם.
    ממוין לפי ROI מהגבוה לנמוך.
    """
    try:
        return await get_sniper_opportunities(limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/safe-profit")
async def safe_profit(
    min_prob: float = Query(80.0, ge=50.0, le=99.9),
    limit:    int   = Query(50,   ge=1,    le=100),
):
    """
    שאלות שהשוק נותן להן הסתברות >min_prob%.
    'הימור בטוח יחסית' — ממוין מהגבוה לנמוך.
    """
    try:
        return await get_safe_profit_markets(min_prob=min_prob, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/whales")
async def whales(
    min_trade_size: float = Query(5000, ge=100),
    limit:          int   = Query(50,   ge=1, le=100),
):
    """
    שאלות עם השקעות לוויתנים — ממוין לפי סכום כולל שהושקע.
    """
    try:
        return await get_whale_markets(min_trade_size=min_trade_size, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
