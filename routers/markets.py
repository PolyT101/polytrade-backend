"""
routers/markets.py - v3
Polymarket proxy — fixed endpoints
"""
from fastapi import APIRouter, Query, HTTPException
import httpx

router = APIRouter()

GAMMA = "https://gamma-api.polymarket.com"
DATA  = "https://data-api.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}

async def pm_get(url: str, params: dict = None):
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get(url, headers=HEADERS, params=params or {})
        r.raise_for_status()
        return r.json()


@router.get("/debug-leaderboard")
async def debug_leaderboard():
    """בדיקת חיבור ל-Polymarket v1/leaderboard מהשרת."""
    import httpx
    results = {}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for tp in ["all", "day"]:
            try:
                r = await client.get(
                    f"{DATA}/v1/leaderboard",
                    params={"limit": 3, "timePeriod": tp, "orderBy": "pnl"},
                    headers=HEADERS
                )
                results[tp] = {
                    "status": r.status_code,
                    "body": r.text[:300],
                }
            except Exception as e:
                results[tp] = {"error": str(e)}
    return results

@router.get("/markets")
async def get_markets(limit: int = Query(100), offset: int = Query(0)):
    try:
        data = await pm_get(f"{GAMMA}/markets", {
            "limit": limit, "offset": offset,
            "active": "true", "closed": "false",
            "order": "volume", "ascending": "false",
        })
        markets = data.get("markets", data) if isinstance(data, dict) else data
        return {"markets": markets, "count": len(markets)}
    except Exception as e:
        raise HTTPException(502, f"Markets error: {e}")

@router.get("/leaderboard")
async def get_leaderboard(
    limit: int = Query(100), offset: int = Query(0),
    period: str = Query("all"), order: str = Query("pnl")
):
    PERIOD_MAP = {"1d": "day", "7d": "week", "30d": "month",
                  "day": "day", "week": "week", "month": "month", "all": "all"}
    tp = PERIOD_MAP.get(period, "all")
    ob = "pnl" if order not in ("pnl", "vol") else order
    try:
        data = await pm_get(f"{DATA}/v1/leaderboard", {
            "limit": min(limit, 500), "offset": offset,
            "timePeriod": tp, "orderBy": ob,
        })
        arr = data if isinstance(data, list) else []
        return {"traders": arr, "count": len(arr)}
    except Exception as e:
        raise HTTPException(502, f"Leaderboard error: {e}")

@router.get("/trader/{address}")
async def get_trader(address: str):
    try:
        return await pm_get(f"{DATA}/profiles/{address}")
    except Exception as e:
        raise HTTPException(502, f"Trader not found: {e}")

@router.get("/trader/{address}/positions")
async def get_positions(address: str, limit: int = Query(50)):
    try:
        data = await pm_get(f"{DATA}/positions", {
            "user": address, "limit": limit, "closed": "false"
        })
        return data if isinstance(data, list) else data.get("positions", [])
    except Exception as e:
        raise HTTPException(502, str(e))
