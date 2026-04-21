"""
routers/scanner.py
------------------
GET /api/scanner/search  — חיפוש שאלות עם פילטרים
"""

from fastapi import APIRouter, Query, HTTPException
from typing import Optional
from services.market_scanner import scan_markets

router = APIRouter()


@router.get("/search")
async def search_markets(
    query:         Optional[str]   = Query(None),
    category:      Optional[str]   = Query(None),
    min_liquidity: Optional[float] = Query(None),
    max_liquidity: Optional[float] = Query(None),
    min_volume:    Optional[float] = Query(None),
    max_volume:    Optional[float] = Query(None),
    min_price:     Optional[float] = Query(None),
    max_price:     Optional[float] = Query(None),
    min_days:      Optional[int]   = Query(None),
    max_days:      Optional[int]   = Query(None),
    limit:         int             = Query(100, ge=1, le=500),
):
    """
    חיפוש שאלות לפי:
    - query: טקסט חופשי
    - category: פוליטיקה / קריפטו / ספורט / פיננסים / אחר
    - min/max_liquidity: טווח נזילות
    - min/max_volume: טווח כמות כסף
    - min/max_price: טווח מחיר YES (0.0–1.0)
    - min/max_days: זמן שנותר בימים
    """
    try:
        return await scan_markets(
            query=query,
            category=category,
            min_liquidity=min_liquidity,
            max_liquidity=max_liquidity,
            min_volume=min_volume,
            max_volume=max_volume,
            min_price=min_price,
            max_price=max_price,
            min_days=min_days,
            max_days=max_days,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
