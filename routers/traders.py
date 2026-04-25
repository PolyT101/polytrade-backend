"""
routers/traders.py
------------------
GET /api/traders          — רשימת טריידרים מהלידרבורד
GET /api/traders/{addr}   — פרופיל טריידר מלא
GET /api/traders/{addr}/positions — פוזיציות
GET /api/traders/{addr}/history   — היסטוריית רווח לגרף
"""

from fastapi import APIRouter, HTTPException, Query
from services.polymarket_service import (
    get_leaderboard, get_trader_profile,
    get_trader_positions, get_profit_history,
)

router = APIRouter()


@router.get("")
async def list_traders(
    limit:  int = Query(100, ge=1, le=500),
    offset: int = Query(0,   ge=0),
    period: str = Query("all"),   # day | week | month | all
    order:  str = Query("pnl"),   # pnl | vol
):
    try:
        return await get_leaderboard(limit=limit, offset=offset,
                                     period=period, order=order)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/{address}")
async def trader_profile(address: str):
    try:
        return await get_trader_profile(address)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/{address}/positions")
async def trader_positions(address: str, closed: bool = False):
    try:
        return await get_trader_positions(address, closed=closed)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/{address}/history")
async def trader_history(address: str):
    try:
        return await get_profit_history(address)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
